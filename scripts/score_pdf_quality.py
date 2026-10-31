#!/usr/bin/env python3
# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0.

"""Score saved PDF candidate outputs in a separate, annotation-aware process.

The candidate runner must never receive the annotation path. This script reads
candidate JSON files produced by ``evaluate_pdf_candidate.py`` and the original
FinanceBench JSONL only after candidate execution has finished. Its report omits
answers, evidence text, and annotated evidence page numbers.

Example::

    python scripts/score_pdf_quality.py --labels /private/financebench.jsonl \
        --candidate-output /tmp/group-01.json /tmp/group-02.json \
        --input-manifest /tmp/inputs.json --independent-text-counts \
        --output /tmp/quality-report.json

FinanceBench's annotated page numbers are zero-based. Candidate page numbers
are one-based physical page numbers. The optional Poppler text counts are an
independent diagnostic; they are not the organizer's frozen reference counts.
The command exits with status 1 when an available annotation quality gate fails.
A successful exit does not establish acceptance against unpublished references.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

TOKEN_PATTERN = re.compile(r"[a-z]+|\d+")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def tokens(text: str) -> Counter[str]:
    return Counter(TOKEN_PATTERN.findall(text.lower()))


def overlap_scores(candidate: Counter[str], reference: Counter[str]) -> tuple[float, float]:
    overlap = sum((candidate & reference).values())
    expected = sum(reference.values())
    actual = sum(candidate.values())
    recall = overlap / expected if expected else 1.0
    f1 = 2 * overlap / (actual + expected) if actual + expected else 1.0
    return recall, f1


def load_candidates(
    paths: list[Path],
) -> tuple[dict[str, Any], dict[str, Any], int, dict[str, tuple[str, ...]]]:
    documents: dict[str, Any] = {}
    questions: dict[str, Any] = {}
    groups: dict[str, tuple[str, ...]] = {}
    physical_pages = 0
    for path in paths:
        result = json.loads(path.read_text(encoding="utf-8"))
        require(result.get("schema_version") == 1, "Unsupported candidate output schema")
        require(result.get("status") == "passed", "Candidate structural execution did not pass")
        for document in result["documents"]:
            document_id = document["document_id"]
            require(document_id not in documents, f"Duplicate candidate document: {document_id}")
            require(
                [page["page_number"] for page in document["pages"]] == list(range(1, document["page_count"] + 1)),
                f"Candidate pages are incomplete or out of order: {document_id}",
            )
            documents[document_id] = document
            physical_pages += document["page_count"]
        for question in result["questions"]:
            question_id = question["question_id"]
            require(question_id not in questions, f"Duplicate candidate question: {question_id}")
            require(question["limit"] == 20, f"Candidate did not request limit 20: {question_id}")
            require(len(question["returned_pages"]) <= 20, f"Candidate exceeded limit 20: {question_id}")
            locations = [(item["document_id"], item["page_number"]) for item in question["returned_pages"]]
            require(len(locations) == len(set(locations)), f"Candidate returned duplicate pages: {question_id}")
            questions[question_id] = question
        group_questions = tuple(question["question_id"] for question in result["questions"])
        require(bool(group_questions), f"Candidate group has no questions: {path}")
        groups[str(path)] = group_questions
    require(bool(documents) and bool(questions), "Candidate outputs need documents and questions")
    return documents, questions, physical_pages, groups


def empty_document_report(document_id: str, page_count: int = 0) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "candidate_physical_pages": page_count,
        "evidence_annotations": 0,
        "evidence_text_recall_min": None,
        "evidence_text_recall_failures": 0,
        "annotated_full_page_f1_min": None,
        "annotated_full_page_f1_failures": 0,
        "missing_annotated_pages": 0,
    }


def score_annotations(
    labels: list[dict[str, Any]],
    documents: dict[str, Any],
    questions: dict[str, Any],
    groups: dict[str, tuple[str, ...]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    document_reports = {
        document_id: empty_document_report(document_id, document["page_count"])
        for document_id, document in documents.items()
    }
    page_tokens = {
        (document_id, page["page_number"]): tokens(page["text"])
        for document_id, document in documents.items()
        for page in document["pages"]
    }
    question_reports = []
    recovered = 0
    expected = 0
    annotation_count = 0
    for label in labels:
        question_id = label["financebench_id"]
        question = questions[question_id]
        require(question["question"] == label["question"], f"Question text differs from reference: {question_id}")
        predicted = {(item["document_id"], item["page_number"]) for item in question["returned_pages"][:20]}
        require(all(location in page_tokens for location in predicted), f"Citation lacks candidate page: {question_id}")
        targets = set()
        recall_failures = 0
        f1_failures = 0
        for evidence in label["evidence"]:
            document_id = evidence["doc_name"]
            page_number = evidence["evidence_page_num"] + 1
            location = (document_id, page_number)
            targets.add(location)
            report = document_reports.setdefault(document_id, empty_document_report(document_id))
            report["evidence_annotations"] += 1
            annotation_count += 1
            candidate = page_tokens.get(location, Counter())
            if location not in page_tokens:
                report["missing_annotated_pages"] += 1
            recall, _ = overlap_scores(candidate, tokens(evidence["evidence_text"]))
            _, f1 = overlap_scores(candidate, tokens(evidence["evidence_text_full_page"]))
            previous_recall = report["evidence_text_recall_min"]
            previous_f1 = report["annotated_full_page_f1_min"]
            report["evidence_text_recall_min"] = recall if previous_recall is None else min(previous_recall, recall)
            report["annotated_full_page_f1_min"] = f1 if previous_f1 is None else min(previous_f1, f1)
            report["evidence_text_recall_failures"] += int(recall < 0.95)
            report["annotated_full_page_f1_failures"] += int(f1 < 0.95)
            recall_failures += int(recall < 0.95)
            f1_failures += int(f1 < 0.95)
        require(bool(targets), f"Reference question has no evidence: {question_id}")
        hits = len(predicted & targets)
        recovered += hits
        expected += len(targets)
        question_reports.append({
            "question_id": question_id,
            "returned_pages": len(predicted),
            "expected_unique_evidence_pages": len(targets),
            "recovered_unique_evidence_pages": hits,
            "recall_at_20": hits / len(targets),
            "evidence_text_recall_failures": recall_failures,
            "annotated_full_page_f1_failures": f1_failures,
        })
    question_results = {item["question_id"]: item for item in question_reports}
    grouped_questions = [question_id for group in groups.values() for question_id in group]
    require(
        len(grouped_questions) == len(set(grouped_questions)) and set(grouped_questions) == set(question_results),
        "Each scored question must belong to exactly one candidate group",
    )
    group_reports = []
    for path, question_ids in groups.items():
        group_expected = sum(
            question_results[question_id]["expected_unique_evidence_pages"] for question_id in question_ids
        )
        group_recovered = sum(
            question_results[question_id]["recovered_unique_evidence_pages"] for question_id in question_ids
        )
        require(group_expected > 0, f"Candidate group has no annotated evidence pages: {path}")
        group_recall = group_recovered / group_expected
        group_reports.append({
            "candidate_output_file": path,
            "questions": len(question_ids),
            "expected_unique_evidence_pages_across_questions": group_expected,
            "recovered_unique_evidence_pages_across_questions": group_recovered,
            "micro_recall_at_20": group_recall,
            "micro_recall_at_20_threshold": 0.70,
            "retrieval_gate_pass": group_recall >= 0.70,
        })
    reports = list(document_reports.values())
    summary = {
        "evidence_annotations": annotation_count,
        "evidence_text_recall_threshold": 0.95,
        "evidence_text_recall_failures": sum(item["evidence_text_recall_failures"] for item in reports),
        "annotated_full_page_f1_threshold": 0.95,
        "annotated_full_page_f1_failures": sum(item["annotated_full_page_f1_failures"] for item in reports),
        "missing_annotated_pages": sum(item["missing_annotated_pages"] for item in reports),
        "expected_unique_evidence_pages_across_questions": expected,
        "recovered_unique_evidence_pages_across_questions": recovered,
        "micro_recall_at_20": recovered / expected if expected else 0.0,
        "micro_recall_at_20_threshold": 0.70,
        "retrieval_groups": len(group_reports),
        "retrieval_groups_below_threshold": sum(not item["retrieval_gate_pass"] for item in group_reports),
        "questions_with_incomplete_evidence_retrieval": sum(item["recall_at_20"] < 1 for item in question_reports),
    }
    summary["available_annotated_quality_gates_pass"] = (
        summary["evidence_text_recall_failures"] == 0
        and summary["annotated_full_page_f1_failures"] == 0
        and summary["missing_annotated_pages"] == 0
        and summary["retrieval_groups_below_threshold"] == 0
    )
    return reports, question_reports, group_reports, summary


def independent_diagnostics(documents: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    executable = shutil.which("pdftotext")
    if executable is None:
        raise ValueError("Independent text diagnostics require Poppler pdftotext")  # noqa: TRY003
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = {item["document_id"]: (manifest_path.parent / item["path"]).resolve() for item in manifest["documents"]}
    reports = []
    for document_id, document in documents.items():
        require(document_id in paths, f"Independent diagnostic lacks document path: {document_id}")
        # The executable is resolved locally; the PDF path is passed as an argument, without a shell.
        process = subprocess.run(  # noqa: S603
            [executable, "-enc", "UTF-8", str(paths[document_id]), "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=120,
        )
        pages = process.stdout.split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        ratios = []
        outside_range = 0
        empty_reference_pages = 0
        unexpected_text_on_empty_pages = 0
        for index, reference_text in enumerate(pages):
            reference_count = sum(tokens(reference_text).values())
            candidate_text = document["pages"][index]["text"] if index < len(document["pages"]) else ""
            candidate_count = sum(tokens(candidate_text).values())
            if reference_count:
                ratio = candidate_count / reference_count
                ratios.append(ratio)
                outside_range += int(not 0.90 <= ratio <= 1.10)
            else:
                empty_reference_pages += 1
                unexpected_text_on_empty_pages += int(candidate_count > 0)
        reports.append({
            "document_id": document_id,
            "independent_physical_pages": len(pages),
            "candidate_physical_pages": document["page_count"],
            "physical_page_coverage_pass": document["page_count"] == len(pages),
            "nonempty_reference_pages": len(ratios),
            "empty_reference_pages": empty_reference_pages,
            "unexpected_text_on_independently_empty_pages": unexpected_text_on_empty_pages,
            "token_count_ratio_min": min(ratios) if ratios else None,
            "token_count_ratio_max": max(ratios) if ratios else None,
            "pages_outside_90_to_110_percent": outside_range,
        })
    return {
        "reference": "Poppler pdftotext UTF-8 default extraction of the complete original PDFs",
        "official_frozen_reference": False,
        "interpretation": "Independent diagnostic only; differences do not establish official gate results",
        "documents": reports,
        "physical_page_coverage_failures": sum(not item["physical_page_coverage_pass"] for item in reports),
        "pages_outside_90_to_110_percent": sum(item["pages_outside_90_to_110_percent"] for item in reports),
        "unexpected_text_on_independently_empty_pages": sum(
            item["unexpected_text_on_independently_empty_pages"] for item in reports
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--candidate-output", required=True, type=Path, nargs="+")
    parser.add_argument("--input-manifest", type=Path)
    parser.add_argument("--independent-text-counts", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    require(
        not arguments.independent_text_counts or arguments.input_manifest is not None,
        "Independent diagnostics require --input-manifest",
    )
    documents, questions, physical_pages, groups = load_candidates(arguments.candidate_output)
    references = {}
    for line in arguments.labels.read_text(encoding="utf-8").splitlines():
        if line.strip():
            reference = json.loads(line)
            question_id = reference["financebench_id"]
            require(question_id not in references, "Duplicate reference question identifier")
            references[question_id] = reference
    require(set(questions) <= set(references), "Candidate has question identifiers absent from reference")
    selected = [references[question_id] for question_id in questions]
    document_reports, question_reports, group_reports, summary = score_annotations(
        selected, documents, questions, groups
    )
    report = {
        "schema_version": 1,
        "candidate_output_files": len(arguments.candidate_output),
        "candidate_documents": len(documents),
        "candidate_physical_pages": physical_pages,
        "scored_questions": len(questions),
        "annotation_tokenization": r"lowercase regex [a-z]+|\d+; Counter multiset overlap",
        "retrieval_aggregation": (
            "Per candidate-output group micro recall over per-question unique (document_id, physical_page) targets; "
            "the overall micro recall is diagnostic and every group must meet its threshold"
        ),
        "official_frozen_all_page_token_count_gate_verified": False,
        "official_complete_acceptance_verified": False,
        "summary": summary,
        "documents": document_reports,
        "questions": question_reports,
        "groups": group_reports,
    }
    if arguments.independent_text_counts:
        report["independent_text_count_diagnostic"] = independent_diagnostics(documents, arguments.input_manifest)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(arguments.output), **summary}, ensure_ascii=False, indent=2))
    if not summary["available_annotated_quality_gates_pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
