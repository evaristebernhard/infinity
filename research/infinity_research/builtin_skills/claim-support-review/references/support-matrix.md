# Support relationships

Official sources checked on 2026-11-05:

- [CNIPA Patent Law, 2020 amendment, Article 26](https://www.cnipa.gov.cn/art/2020/11/23/art_97_155167.html): claims are based on the description and define the requested scope clearly and concisely.
- [WIPO PCT ISPE Guidelines 5.43-5.44](https://www.wipo.int/en/web/pct-system/texts/ispe/5_43_44): every claim needs a basis in the description, and its scope must be justified by the description and drawings. Vague assertions without relevant technical content do not establish that basis.
- [WIPO PCT Rule 6](https://www.wipo.int/en/web/pct-system/texts/rules/r6): claims express technical features; references to the description or drawings generally cannot replace specifying those features. A matrix is an internal review aid, not a substitute for claim text.

Support review and patentability analysis are separate tasks. A complete matrix
can still contain disputed interpretations, insufficient implementation detail,
or unsupported breadth. Retain those findings rather than reducing the review
to a positive Boolean result.

This synthetic example illustrates a partial match. Its references and interval
are invented only for the schema example and must never be cited as evidence.

```json
{
  "claim_revision": "example-claims-r1",
  "description_revision": "example-description-r2",
  "claim_id": "1",
  "feature_id": "C1-F2",
  "claimed_feature": "Update the valve command at intervals below 1 ms",
  "description_refs": [{"artifact_id": "example-description-r2", "locator": "section 3"}],
  "figure_refs": [],
  "support_kind": "partial",
  "review_state": "needs_review",
  "reason": "The passage describes updating the command but specifies no interval",
  "combination_review": "pending",
  "next_action": "Locate an original basis for the interval or propose a claim revision"
}
```
