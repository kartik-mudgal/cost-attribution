---
name: cur-bedrock
description: >
  Bedrock cost analysis from CUR: input, output and cache tokens by model,
  marketplace model lines, and the invocation metrics that explain token growth.
  Load after cur-athena when the service in scope is Bedrock.
---

# Bedrock in CUR

Two product codes carry this spend. Query both before you say Bedrock is small.

- `line_item_product_code = 'AmazonBedrock'` for first-party Bedrock usage types.
- Marketplace models. `product['product_name']` contains `Bedrock`. Their `line_item_product_code` is a hash, not `AmazonBedrock`.

October 2026 in this account the large lines were marketplace editions (GPT and Claude "Amazon Bedrock Edition"), not `AmazonBedrock`. The `AmazonBedrock` line itself was mostly `Bedrock-Websearch-Queries` plus a small token line. Re-query before citing dollars.

Count every token type. Output, input, cache read and cache write are separate rates. Missing one understates cost.

## Top 5 cost drivers

1. Output tokens. Several times the input rate. Long answers and agent loops dominate.
2. Input tokens. System prompts, history, retrieved documents and tool definitions are resent on every call. Cache reads are about a tenth of the input price when prefixes repeat.
3. Model and tier. The same tokens cost very different rates by model. Priority, Flex, Batch and cross-region suffixes change the rate again.
4. Provisioned Throughput. Hourly model units whether or not they are used.
5. Retries and adjacent features. Failed calls resend input tokens. Guardrails, Knowledge Bases and web search are separate lines.

## Top 5 metrics

Namespace `AWS/Bedrock`, dimension `ModelId`.

1. `OutputTokenCount`
2. `InputTokenCount`
3. `CacheReadInputTokenCount` and `CacheWriteInputTokenCount`
4. `Invocations`
5. `InvocationThrottles`, `InvocationClientErrors` and `InvocationServerErrors`

Throttled calls are not in `Invocations`. CloudWatch does not say which application sent the prompt. CUR can, via `line_item_iam_principal` and application inference profile tags, only if those fields are in the export.

## What CUR alone can show

| Check | Usage type |
| --- | --- |
| Input tokens | `*-input-tokens` |
| Output tokens | `*-output-tokens` |
| Cache | `*-cache-read-input-token-count`, `*-cache-write-input-token-count` |
| Tier | suffixes `-priority`, `-flex`, `-cross-region-global` |
| Provisioned | `ProvisionedThroughput` |
| Web search | `Bedrock-Websearch-Queries` |

A shared gateway role cannot be split by CUR. Label that split estimated.

## Example: marketplace Bedrock editions

```sql
SELECT product['product_name'] AS product_name,
       line_item_usage_type,
       ROUND(SUM(line_item_usage_amount), 0) AS usage,
       ROUND(SUM(line_item_unblended_cost), 2) AS cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_line_item_type = 'Usage'
  AND product['product_name'] LIKE '%Bedrock%'
GROUP BY 1, 2
ORDER BY cost DESC
LIMIT 30
```

## Example: first-party AmazonBedrock lines

```sql
SELECT line_item_usage_type,
       ROUND(SUM(line_item_usage_amount), 0) AS usage,
       ROUND(SUM(line_item_unblended_cost), 2) AS cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_product_code = 'AmazonBedrock'
  AND line_item_line_item_type = 'Usage'
GROUP BY 1
ORDER BY cost DESC
```
