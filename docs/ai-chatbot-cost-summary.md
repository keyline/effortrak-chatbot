# Effortrak Chatbot: Simple Cost Guide

For management | 27 October 2026 | All amounts in USD

## Quick Answer

If a company uses its full 100,000-token allowance, GPT-4.1 Mini costs $0.052 in the example below (about 5 US cents). To budget conservatively, allow up to $0.16 per company for those tokens. Taxes and hosting are extra.

"Free tokens" are free for your customer. Effortrak pays OpenAI for actual usage. Unused tokens cost nothing.

## What Is a Token?

A token is a small piece of text, not a question. One question can use thousands of tokens.

| Token Type | Simple Meaning |
| --- | --- |
| Input | The question plus application instructions and context sent to OpenAI |
| Output | Text or structured selections returned by OpenAI |
| Cached input | Input eligible for a discounted reuse rate; not extra tokens |

The allowance means input and output added together. Report text produced by our backend is not automatically OpenAI output.

## Which Questions Cost Money?

| Request | OpenAI Cost |
| --- | --- |
| Recognized help question: "How do I add an employee?" | No OpenAI cost |
| Unfamiliar help wording sent to the optional AI fallback | Paid token usage |
| Report or chart question interpreted by AI | Paid token usage |
| Simply enabling the chatbot for a company | No usage cost by itself |

Some requests are handled directly without OpenAI. There is no fixed charge per question.

## Model Pricing

Standard prices per 1 million tokens:

| Model | Input | Cached Input | Output |
| --- | ---: | ---: | ---: |
| GPT-4.1 Mini | $0.40 | $0.10 | $1.60 |
| GPT-4.1 | $2.00 | $0.50 | $8.00 |

GPT-4.1 costs five times more for the same token usage and caching. Example: 3,000 input + 200 output tokens cost $0.00152 on Mini or $0.00760 on GPT-4.1, without caching. This is an example, not a measured customer average.

## Cost of 100,000 Tokens per Company

These examples assume the company uses all 100,000 tokens, with no caching discount.

| Input Tokens | Output Tokens | GPT-4.1 Mini | GPT-4.1 |
| ---: | ---: | ---: | ---: |
| 100,000 | 0 | $0.040 | $0.200 |
| 90,000 | 10,000 | $0.052 | $0.260 |
| 80,000 | 20,000 | $0.064 | $0.320 |
| 0 | 100,000 | $0.160 | $0.800 |

The last row is a conservative maximum: it prices every token at the more expensive output rate. This is a budgeting limit, not normal usage. Caching discounts may reduce the bill.

## Monthly Budget

Assume each company uses all 100,000 tokens every month. The example columns use 90,000 input + 10,000 output per company.

| Companies | Mini Example | GPT-4.1 Example | Mini Maximum |
| ---: | ---: | ---: | ---: |
| 100 | $5.20 | $26.00 | $16.00 |
| 1,000 | $52.00 | $260.00 | $160.00 |
| 10,000 | $520.00 | $2,600.00 | $1,600.00 |

The Mini maximum column uses the more expensive output rate for every token. Actual costs depend on usage.

How many questions does this cover? At 3,200 tokens per paid question, 100,000 tokens cover about 31 questions. Actual usage varies, and one question may make multiple AI calls. Recognized help answers do not use the allowance.

## Is the Larger Model Better?

A larger model may understand difficult questions better. It cannot fix missing data, incorrect calculations or access permissions. Test real customer questions before upgrading, and confirm the model works with the application.

## Recommendation

Start with Mini and a 100,000-token monthly allowance per company. Enforce the limit and measure actual costs before expanding. This is a proposal; this document does not change application settings.

All estimates exclude hosting, taxes, exchange-rate changes and payment fees. Prices and model availability can change. If usage beyond the allowance is allowed, it adds cost. The maximums above cover only the stated model tokens, not other paid services.

## Official Sources

- GPT-4.1 Mini: https://developers.openai.com/api/docs/models/gpt-4.1-mini
- GPT-4.1: https://developers.openai.com/api/docs/models/gpt-4.1

