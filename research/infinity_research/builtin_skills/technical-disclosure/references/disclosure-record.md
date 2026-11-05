# Disclosure structure and feature ledger

Official sources checked on 2026-11-05:

- [CNIPA Implementing Regulations, 2023 revision, Article 20](https://www.cnipa.gov.cn/art/2023/12/21/art_98_189197.html): description sections cover the technical field, background, invention content, drawings, and embodiments.
- [CNIPA Patent Law, 2020 amendment, Article 26](https://www.cnipa.gov.cn/art/2020/11/23/art_97_155167.html): the description must explain the invention clearly and completely enough for a person skilled in the art to implement it.
- [WIPO PCT Rule 5](https://www.wipo.int/en/web/pct-system/texts/rules/r5): the description connects the technical problem, solution, drawings, and ways of carrying out the invention. Account for jurisdiction-specific requirements before treating a template as a filing instruction.

This synthetic example illustrates provenance and an untested effect. It is not
an invention record, an experiment, or evidence of adequate disclosure.

```json
{
  "feature_id": "F-001",
  "definition": "Adjust the valve command using measured pressure error",
  "source_refs": [{"artifact_id": "example-notes-r1", "locator": "section 2"}],
  "evidence_kind": "source_statement",
  "evidence_state": "reported",
  "problem": "Pressure overshoot during a load change",
  "mechanism": "Feed the pressure error into the valve command update",
  "effect": {
    "statement": "The update may reduce pressure overshoot",
    "basis": "inference",
    "evidence_refs": [],
    "experiment_status": "not_performed"
  },
  "implementation_gaps": [
    "The update equation and sampling interval are not specified",
    "No baseline or measured comparison is supplied"
  ]
}
```
