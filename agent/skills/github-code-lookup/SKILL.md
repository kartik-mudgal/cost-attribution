---
name: github-code-lookup
description: >
  Access DevRev GitHub repositories. Use when you need to read or search code
  in the devrev org. Auth is a PAT loaded from AWS Secrets Manager — never
  print the token.
---

# GitHub access

GitHub tools load a PAT from Secrets Manager and use it for the `devrev` org.
Do not ask for a token or echo it.
