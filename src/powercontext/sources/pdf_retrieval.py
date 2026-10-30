# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Offline, question-driven ranking of complete PDF pages held in scoped Memory."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory import MemoryCitation
    from powercontext.builtin.runtime.application import ScopedMemoryApplication
    from powercontext.builtin.runtime.models import MemoryEntryRecord

# Query boilerplate carries little evidence-page information. Keep years,
# company names, amounts and financial nouns rather than filtering rare words.
_STOP_WORDS = frozenset([
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "may",
    "me",
    "of",
    "on",
    "or",
    "our",
    "please",
    "should",
    "than",
    "that",
    "the",
    "their",
    "them",
    "there",
    "these",
    "they",
    "this",
    "those",
    "to",
    "us",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "whether",
    "which",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
    "any",
    "all",
    "also",
    "about",
    "according",
    "annual",
    "answer",
    "analyst",
    "available",
    "based",
    "calculate",
    "concise",
    "consider",
    "defined",
    "explicitly",
    "explanation",
    "financial",
    "following",
    "give",
    "information",
    "investment",
    "million",
    "millions",
    "perspective",
    "provide",
    "question",
    "report",
    "reported",
    "reports",
    "rely",
    "say",
    "says",
    "statement",
    "statements",
    "taking",
    "tell",
    "using",
    "use",
    "value",
    "values",
    "year",
    "fiscal",
    "fy",
    "respectively",
    "expressed",
    "unadjusted",
])
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)

# These are general accounting concepts, independent of any company, period or
# benchmark question. Each concept contributes separately to multi-statement
# questions, so a cash-flow page can complement an income-statement page.
_CONCEPTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        r"\b(?:geograph\w*|geographi\w*|countries|country|regions?|territor\w*|worldwide)\b",
        (
            "geographic",
            "geography",
            "region",
            "country",
            "international",
            "segment",
            "market",
            "operations",
        ),
    ),
    (
        r"\b(?:legal|litigat\w*|lawsuits?|proceedings?|battles?|court\w*|contingenc\w*)\b",
        (
            "legal",
            "litigation",
            "proceeding",
            "lawsuit",
            "contingency",
            "settlement",
            "claim",
            "court",
        ),
    ),
    (
        r"\b(?:restructur\w*|reorganiz\w*)\b",
        (
            "restructuring",
            "reorganization",
            "charge",
            "severance",
            "impairment",
            "productivity",
        ),
    ),
    (
        r"\b(?:ebitda|operating\s+(?:income|profit)|income\s+statements?|statements?\s+of\s+income|"
        r"p\s*&\s*l|profit\s+and\s+loss)\b",
        (
            "operating income",
            "operating profit",
            "income",
            "earnings",
            "operations",
            "consolidated",
        ),
    ),
    (
        r"\b(?:ebitda|depreciat\w*|amortiz\w*|amortis\w*|d\s*&\s*a)\b",
        (
            "depreciation",
            "amortization",
            "cash flows",
            "operating activities",
        ),
    ),
    (
        r"\b(?:capex|capital\s+(?:expenditur\w*|spend\w*)|cash\s*flows?)\b",
        (
            "cash flows",
            "capital expenditures",
            "capital spending",
            "property plant equipment",
            "investing activities",
        ),
    ),
    (r"\b(?:revenue\w*|sales|margin\w*)\b", ("revenue", "sales", "net revenue", "income")),
    (
        r"\b(?:balance\s+sheet|financial\s+position|assets?|liabilit\w*)\b",
        (
            "balance sheet",
            "financial position",
            "asset",
            "liability",
            "equity",
        ),
    ),
    (
        r"\b(?:debts?|borrow\w*|leverage|indebtedness)\b",
        (
            "debt",
            "borrowing",
            "maturity",
            "interest",
            "notes",
            "credit",
        ),
    ),
    (
        r"\b(?:dividends?|repurchas\w*|buybacks?)\b",
        (
            "dividend",
            "repurchase",
            "common stock",
            "shareholder",
            "financing activities",
        ),
    ),
)

# Ratios are commonly named in a question but absent from the original
# financial statements. Retrieve their underlying accounting line items and
# the complementary statements needed to calculate them.
_ACCOUNTING_METRICS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        r"\b(?:current\s+ratio|working\s+capital)\b",
        ("current assets", "current liabilities"),
        ("balance",),
    ),
    (
        r"\bquick\s+ratio\b",
        ("cash", "accounts receivable", "short term investments", "current assets", "inventory", "current liabilities"),
        ("balance",),
    ),
    (
        r"\b(?:return\s+on\s+(?:common\s+|shareholders?\s+)?equity|roe)\b",
        ("net income", "shareholders equity", "stockholders equity"),
        ("income", "balance"),
    ),
    (
        r"\b(?:return\s+on\s+(?:total\s+)?assets?|roa)\b",
        ("net income", "total assets"),
        ("income", "balance"),
    ),
    (
        r"\b(?:return\s+on\s+invested\s+capital|roic)\b",
        ("operating income", "income taxes", "debt", "equity", "cash"),
        ("income", "balance"),
    ),
    (
        r"\b(?:total\s+)?assets?\s+turnover\b",
        ("revenue", "net sales", "total assets"),
        ("income", "balance"),
    ),
    (
        r"\b(?:interest\s+coverage|times\s+interest\s+earned)\b",
        ("operating income", "interest expense", "income before taxes"),
        ("income",),
    ),
    (
        r"\b(?:effective\s+(?:income\s+)?tax\s+rate|etr)\b",
        ("income tax expense", "income before taxes"),
        ("income",),
    ),
    (
        r"\bnet\s+(?:(?:income|profit)\s+)?margin\b",
        ("net income", "revenue", "net sales"),
        ("income",),
    ),
    (
        r"\bgross\s+(?:profit\s+)?margin\b",
        ("gross profit", "revenue", "net sales", "cost of goods sold"),
        ("income",),
    ),
    (
        r"\boperating\s+(?:profit\s+)?margin\b",
        ("operating income", "operating profit", "revenue", "net sales"),
        ("income",),
    ),
)

_DOCUMENT_WORDS = frozenset({
    "annual",
    "report",
    "form",
    "10k",
    "10q",
    "20f",
    "40f",
    "k",
    "q",
    "f",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "company",
    "co",
    "ltd",
    "limited",
    "plc",
    "holding",
    "group",
})
_HEADING_PREFIX = r"(?im)^\s*(?:(?:consolidated|condensed|combined|unaudited|audited)\s+)*"
# Anchored headings distinguish a statement itself from notes discussing it.
# These roles are derived from original page text, never from page-number lists.
_STATEMENT_PATTERNS = {
    "income": _HEADING_PREFIX + r"(?:statements?\s+of\s+(?:income|operations|earnings|profit(?:\s+and\s+loss)?)|"
    r"(?:income|profit\s+and\s+loss)\s+statements?)\b",
    "cashflow": _HEADING_PREFIX + r"(?:statements?\s+of\s+cash\s+flows?|cash\s+flow\s+statements?)\b",
    "balance": _HEADING_PREFIX + r"(?:balance\s+sheets?|statements?\s+of\s+financial\s+position)\b",
    "equity": _HEADING_PREFIX + r"statements?\s+of\s+(?:changes\s+in\s+)?(?:stockholders|shareholders|owners|equity)\b",
    "comprehensive": _HEADING_PREFIX + r"statements?\s+of\s+(?:other\s+)?comprehensive\s+income\b",
}
_STATEMENT_QUESTIONS = {
    "income": r"\b(?:ebitda|ebit|operating\s+(?:income|profits?)|(?:gross|net|profit)\s+margin|"
    r"income\s+statements?|statements?\s+of\s+(?:income|operations|earnings)|p\s*&\s*l|profit\s+and\s+loss)\b",
    "cashflow": r"\b(?:ebitda|cash\s*flows?|capex|capital\s+expenditur\w*|depreciat\w*|amorti[sz]\w*|d\s*&\s*a)\b",
    "balance": r"\b(?:balance\s+sheets?|financial\s+position|current\s+ratio|quick\s+ratio|working\s+capital|"
    r"total\s+(?:assets|liabilities)|debt\s+to\s+equity)\b",
    "equity": r"\bstatements?\s+of\s+(?:changes\s+in\s+)?(?:stockholders|shareholders|owners|equity)\b",
    "comprehensive": r"\b(?:other\s+comprehensive\s+income|comprehensive\s+income\s+statements?)\b",
}


@dataclass(frozen=True)
class _Page:
    record: MemoryEntryRecord
    identity: tuple[str, str]
    terms: Counter[str]
    tokens: tuple[str, ...]
    normalized_text: str
    document_id: str
    statements: dict[str, int]


def _stem(term: str) -> str:
    # A small conservative English normalizer avoids an extra NLP/model
    # dependency while keeping numerical tokens and short identifiers intact.
    if term.endswith("ies") and len(term) > 4:
        return term[:-3] + "y"
    if term.endswith("sses"):
        return term[:-2]
    if term.endswith("s") and len(term) > 3 and not term.endswith(("ss", "us", "is")):
        return term[:-1]
    return term


def _tokens(text: str) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"\bfy\s*(\d{2,4})\b", r"\1", text)
    text = text.replace("amortisation", "amortization")
    return tuple(_stem(term) for term in _TOKEN.findall(text))


def _query(question: str) -> tuple[dict[str, float], tuple[dict[str, float], ...], tuple[str, ...]]:
    normalized = unicodedata.normalize("NFKC", question).casefold()
    original = {term: 1.0 for term in _tokens(question) if term not in _STOP_WORDS}
    weights = dict(original)
    facets = []
    phrases = []
    concepts = (*_CONCEPTS, *((pattern, terms) for pattern, terms, _ in _ACCOUNTING_METRICS))
    for pattern, expansions in concepts:
        if not re.search(pattern, normalized):
            continue
        facet = {}
        for expansion in expansions:
            for term in _tokens(expansion):
                facet[term] = 1.0
                weights[term] = max(weights.get(term, 0.0), 0.35)
            if " " in expansion:
                phrases.append(expansion)
        facets.append(facet)
    return weights, tuple(facets), tuple(phrases)


def _statement_headings(text: str) -> dict[str, int]:
    heading = unicodedata.normalize("NFKC", "\n".join(text.splitlines()[:35]))
    roles = {}
    for role, pattern in _STATEMENT_PATTERNS.items():
        for match in re.finditer(pattern, heading):
            remainder = heading[match.end() :]
            # Contents listings carry dot leaders or an isolated physical/
            # printed page reference. A repeated "Table of Contents" link at
            # the top of a genuine statement does not make it a contents page.
            if re.match(r"[ \t]*\.{2,}", remainder) or re.match(r"[ \t]*(?:\n[ \t]*)?\d{1,3}[ \t]*(?:\n|$)", remainder):
                continue
            title = match.group().casefold()
            qualified = re.search(r"\b(?:consolidated|condensed|combined)\b", title) is not None
            strength = 3 if qualified else (2 if re.search(r"statements?\s+of\b", title) else 1)
            roles[role] = max(roles.get(role, 0), strength)
    # Exhibit indexes also list several statements with dates, without dot
    # leaders or page references. A real combined financial table has numeric
    # column rows; keep those while excluding a narrative list of statements.
    if len(roles) >= 3 and not re.search(
        r"(?m)^[ \t]*[\d,.$()\u2212\u2013-]+(?:[ \t]+[\d,.$()\u2212\u2013-]+)+[ \t]*$", heading
    ):
        return {}
    return roles


def _document_name(document_id: str) -> tuple[str, ...]:
    return tuple(
        term
        for term in _tokens(document_id)
        if not term.isdigit() and term not in _DOCUMENT_WORDS and term not in _STOP_WORDS
    )


def _route_pages(pages: tuple[_Page, ...], question: str) -> tuple[_Page, ...]:
    # Only an explicit company name inferred from the stored identifiers routes
    # a query. A question without such a name continues to search the full scope.
    raw_words = _tokens(question)
    words = tuple(term for term in raw_words if term not in _STOP_WORDS and term not in _DOCUMENT_WORDS)
    names = {
        "".join(tokens[start : start + width])
        for tokens in (raw_words, words)
        for start in range(len(tokens))
        for width in range(1, 5)
    }
    documents = {page.document_id: _document_name(page.document_id) for page in pages}
    matched: dict[tuple[str, ...], set[str]] = {}
    financial_terms = {
        term
        for expansions in (
            *(terms for _, terms in _CONCEPTS),
            *(terms for _, terms, _ in _ACCOUNTING_METRICS),
        )
        for expansion in expansions
        for term in _tokens(expansion)
    }
    for document_id, company in documents.items():
        # An identifier such as EQUITY must not make an ordinary financial
        # noun into an explicit company request and discard the other reports.
        if company and all(term in financial_terms for term in company):
            continue
        aliases = {"".join(company)}
        if len(company) > 1:
            acronym = "".join(term[0] for term in company)
            if acronym not in _STOP_WORDS and acronym not in _DOCUMENT_WORDS:
                aliases.add(acronym)
        if company and (aliases - {""}) & names:
            matched.setdefault(company, set()).add(document_id)
    if not matched:
        return pages
    years = {term for term in _tokens(question) if re.fullmatch(r"(?:19|20|21)\d{2}", term)}
    preferred = set()
    for documents_in_company in matched.values():
        current = {doc for doc in documents_in_company if years.intersection(_tokens(doc))}
        preferred.update(current or documents_in_company)
    return tuple(page for page in pages if page.document_id in preferred)


def _pages(records: tuple[MemoryEntryRecord, ...]) -> tuple[_Page, ...]:
    pages = []
    for record in records:
        sources = {(source.source_type, source.source_id) for source in record.entry.sources}
        pdf_sources = {source for source in sources if source[0] == "pdf-page"}
        # A multi-page synthesis is not an unambiguous physical-page citation.
        if len(pdf_sources) != 1 or len(sources) != 1:
            continue
        header, separator, body = record.entry.text.partition("\n")
        # Full-page projections prepend the caller's document identifier. Use
        # it for routing, not as evidence on otherwise empty physical pages.
        if not separator:
            continue
        tokens = _tokens(body)
        pages.append(
            _Page(
                record=record,
                identity=next(iter(pdf_sources)),
                terms=Counter(tokens),
                tokens=tokens,
                normalized_text=" ".join(tokens),
                document_id=header,
                statements=_statement_headings(body),
            )
        )
    return tuple(pages)


def _statistics(pages: tuple[_Page, ...]) -> tuple[dict[str, float], float]:
    # A page with multiple Memory entries counts once in collection statistics.
    unique: dict[tuple[str, str], set[str]] = {}
    lengths: dict[tuple[str, str], int] = {}
    for page in pages:
        unique.setdefault(page.identity, set()).update(page.terms)
        lengths[page.identity] = max(lengths.get(page.identity, 0), len(page.tokens))
    frequency: Counter[str] = Counter()
    for terms in unique.values():
        frequency.update(terms)
    count = len(unique)
    idf = {term: math.log1p((count - df + 0.5) / (df + 0.5)) for term, df in frequency.items()}
    return idf, max(1.0, sum(lengths.values()) / count)


def _score(page: _Page, query: dict[str, float], idf: dict[str, float], average_length: float) -> float:
    length_factor = 1.2 * (0.35 + 0.65 * len(page.tokens) / average_length)
    score = 0.0
    matched = set()
    for term, weight in query.items():
        frequency = page.terms.get(term, 0)
        if frequency:
            score += weight * idf[term] * frequency * 2.2 / (frequency + length_factor)
            matched.add(term)
    if not matched:
        return 0.0
    # Dense local evidence outranks a page scattering the same terms over a
    # long index or unrelated footnotes. Work on windows, never truncate text.
    best_window = 0.0
    for start in range(0, len(page.tokens), 32):
        window = set(page.tokens[start : start + 64])
        best_window = max(best_window, sum(query[term] * idf[term] for term in matched & window))
    return score + 0.3 * best_window


def _rank_pages(
    corpus: tuple[_Page, ...],
    query: dict[str, float],
    facets: tuple[dict[str, float], ...],
    phrases: tuple[str, ...],
    statement_roles: tuple[str, ...],
    limit: int,
) -> tuple[MemoryCitation, ...]:
    idf, average_length = _statistics(corpus)
    # Select the best real entry per physical source, rather than deduplicating
    # text (different pages and revisions may legitimately have identical text).
    ranked: dict[tuple[str, str], tuple[float, _Page]] = {}
    facet_scores: list[dict[tuple[str, str], float]] = [{} for _ in facets]
    for page in corpus:
        score = _score(page, query, idf, average_length)
        if score <= 0:
            continue
        for phrase in phrases:
            if " ".join(_tokens(phrase)) in page.normalized_text:
                score += 0.4 * sum(idf.get(term, 0.0) for term in _tokens(phrase))
        previous = ranked.get(page.identity)
        if previous is None or score > previous[0]:
            ranked[page.identity] = (score, page)
        for scores, facet in zip(facet_scores, facets, strict=True):
            scores[page.identity] = max(scores.get(page.identity, 0.0), _score(page, facet, idf, average_length))
    if not ranked:
        return ()
    ordered = _fuse_rankings(ranked, facet_scores)
    selected = _statement_candidates(ranked, statement_roles, limit)
    for key in ordered:
        if len(selected) >= limit:
            break
        selected.add(key)
    return tuple(ranked[key][1].record.citation for key in ordered if key in selected)[:limit]


def _fuse_rankings(
    ranked: dict[tuple[str, str], tuple[float, _Page]], facet_scores: list[dict[tuple[str, str], float]]
) -> list[tuple[str, str]]:
    ordered = sorted(ranked, key=lambda key: (-ranked[key][0], key))
    # Reciprocal-rank fusion preserves secondary evidence for questions needing
    # several accounting statements without letting query expansion replace the
    # original question. Every candidate still comes from the scoped corpus.
    fused = {key: 1.0 / (30 + rank) for rank, key in enumerate(ordered, start=1)}
    for scores in facet_scores:
        facet_order = sorted((key for key in scores if scores[key] > 0), key=lambda key: (-scores[key], key))
        for rank, key in enumerate(facet_order, start=1):
            fused[key] += 0.5 / max(1, len(facet_scores)) / (30 + rank)
    ordered.sort(key=lambda key: (-fused[key], -ranked[key][0], key))
    return ordered


def _statement_candidates(
    ranked: dict[tuple[str, str], tuple[float, _Page]], roles: tuple[str, ...], limit: int
) -> set[tuple[str, str]]:
    # Multi-statement financial questions need complementary evidence. Reserve
    # a small part of the result budget for each requested original statement;
    # fill the rest from content relevance, including explanatory notes.
    selected = set()
    per_role = min(2, max(1, limit // (2 * max(1, len(roles)))))
    for role in roles:
        candidates = [key for key, (_, page) in ranked.items() if role in page.statements]
        candidates.sort(key=lambda key: (-ranked[key][1].statements[role], -ranked[key][0], key))
        selected.update(candidates[:per_role])
    return selected


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Rank active PDF evidence in this scope and return exact, distinct page citations.

    This offline path reads the current scoped Memory corpus through its public
    API. Collection statistics are rebuilt per call so retirement, document
    revisions and Runtime restarts cannot leave a stale external index.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("PDF page limit must be a nonnegative integer")  # noqa: TRY003
    if not isinstance(question, str) or not question.strip():
        raise ValueError("PDF question must not be empty or blank")  # noqa: TRY003
    if limit == 0:
        return ()
    query, facets, phrases = _query(question)
    if not query:
        return ()
    corpus = _route_pages(_pages((await memory.list()).entries), question)
    if not corpus:
        return ()
    normalized = unicodedata.normalize("NFKC", question).casefold()
    roles = {role for role, pattern in _STATEMENT_QUESTIONS.items() if re.search(pattern, normalized)}
    for pattern, _, metric_roles in _ACCOUNTING_METRICS:
        if re.search(pattern, normalized):
            roles.update(metric_roles)
    return _rank_pages(corpus, query, facets, phrases, tuple(sorted(roles)), limit)
