# 论文摘录：Attention Is All You Need（Vaswani et al., 2017）

## 核心观点

- 完全用注意力机制替代 RNN/CNN 的循环与卷积结构，即 Transformer。
- 引用原文："A novel network architecture, Transformer, based solely on attention mechanisms."

## Scaled Dot-Product Attention

- Attention(Q, K, V) = softmax(QK^T / sqrt(d_k)) V。
- 除以 sqrt(d_k) 是为了抵消点积在高维度下量级过大、softmax 梯度过小的问题。

## Multi-Head Attention

- h = 8 个头，每个头在不同投影子空间并行做注意力，再拼接。
- 多头注意力让模型同时关注不同位置、不同表示子空间的信息。

## 位置编码

- 没有循环结构后必须显式注入位置信息：正弦/余弦位置编码。
- PE(pos, 2i) = sin(pos / 10000^(2i/d_model))。

## 训练细节

- 优化器 Adam，学习率先线性 warmup 4000 步再按步数^-0.5 衰减。
- Label smoothing 0.1，提升稳定但也略微降低 perplexity 拟合。
