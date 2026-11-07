# Prototype: audit by selecting anchored references

Run from this branch: `PYTHONPATH=src python3 scripts/notes-audit-prototype.py`.
The TUI keeps state in memory. `p` selects a supported prefix, `x` tries invented text,
and `q` exits. No database or provider is used by the TUI.

Question: can an audit use the existing Notes JSON shape while selecting immutable draft
text/prefixes or whole-source excerpts, instead of generating another free rewrite?

The pure catalog/apply functions are in `src/asr_studio/notes_audit_prototype.py`.
The TUI and this experiment remain on a throwaway branch, outside the implementation PR.

One public-data provider probe used the frozen VietMed source and a previously generated
failing draft. It made one Antigravity/gemini-3.8-flash high call, 223.077 seconds, with
175 catalog references. The prototype accepted the structural reference contract and the
model removed the unsupported named-procedure tail. It retained a disputed clinical
examination qualifier. The experiment therefore validates selection and rejection of new
text, and demonstrates only a partial semantic improvement. It does not certify medical
accuracy or source support for every retained draft claim.

The probe source revision is `af81fdd` (before formatting-only cleanup). Raw source, draft,
prompt, response and result remain local; no derived medical Notes are published here.

Validated implementation decision: an audit can be restricted to allowed references, so
it cannot add arbitrary prose or action metadata. Retained claims still need source review.
A production implementation must preserve modality, bound catalog/prompt sizes, apply the
shared repair limit, handle quote citations by source position, and fail before publication
when a reference contract is invalid. The TUI must not ship in the application branch.
