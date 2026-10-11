---
name: github-code-lookup
description: >
  Access DevRev GitHub repositories. Use when you need to read or search code
  in the devrev org. Auth comes from AgentCore Identity provider github-pat —
  never print the token.
---

# GitHub access

Fetch a GitHub PAT via AgentCore Identity (`github-pat`) and use the GitHub tools
to access repos in the `devrev` org. Do not ask for a token or echo it.
