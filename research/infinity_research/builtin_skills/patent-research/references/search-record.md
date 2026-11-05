# Search provenance

Official sources checked on 2026-11-05:

- [CNIPA Implementing Regulations, 2023 revision, Article 20](https://www.cnipa.gov.cn/art/2023/12/21/art_98_189197.html): the description includes background useful for understanding, searching, and examining the invention, with relevant documents cited where possible.
- [WIPO PCT Rule 5.1](https://www.wipo.int/en/web/pct-system/texts/rules/r5): the description identifies relevant background art and explains the technical problem and solution.

These provisions explain why traceable background evidence belongs in the
workflow. They do not certify the completeness of a search or decide whether
any retrieved document defeats a claim. Recheck the relevant official text when
the task depends on a changed rule or a specific jurisdiction.

The following is a synthetic schema example, not an executed search or a real
publication record. Replace example IDs with actual artifact references.

```json
{
  "question": "Find disclosed mechanisms for pressure feedback control",
  "date_cutoff": null,
  "coverage": "supplied_material_only",
  "search_log": [
    {
      "query": "pressure feedback valve control",
      "database": null,
      "tool": null,
      "status": "planned",
      "executed_at": null,
      "result_refs": []
    }
  ],
  "sources": [
    {
      "source_id": "S-001",
      "origin": "user_supplied",
      "artifact_id": "example-note",
      "access_scope": "supplied_excerpt",
      "locator": "page 2",
      "publication_number": null,
      "publication_date": null,
      "sha256": null
    }
  ],
  "gaps": ["No live search was performed; publication coverage is unassessed"]
}
```
