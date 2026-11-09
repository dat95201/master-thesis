# report/

LaTeX source for the thesis (working title in `metadata.tex`),
**Cutting the Cost of LLM-Based Program Repair with a Cost-Aware Validation
Layer**.

Self-contained: it reads nothing outside this folder. The code that implements
the proposal is in `../vlayer/` (see `../README.md`).

```bash
make          # -> main.pdf   (latexmk + pdflatex)
tectonic main.tex             # alternative without a TeX Live install
make clean
```

## Status

| chapter | state |
|---|---|
| Abstract | final |
| 1 Introduction — problem statement (context, three wastes, goal), objectives + RQ1–RQ4, contributions, scope table | final |
| 2 Background — cost model in two layers, ordering problem, censoring | final |
| 3 Related Works — systems, validation in practice (6 open-source agents + deployed systems), cost measurement, taxonomy, test prioritisation (incl. ExpressAPR, PIT, FaMT), research gap + O1–O5 | final |
| 4 Proposed System — CI repair-bot scenario, requirements P1–P5, validation layer, M1 Repeat Guard, M2 Test Prioritiser (ARC), M3 Budget Guard (simplified) | final |
| 5 Evaluation Plan — Defects4J (20 dev + 100 eval bugs), method-level perfect FL; five orderings (Default, FRTP, Coverage/PIT rule, ARC, oracle); E1 mutants (Major, strata S1–S4), E2 live orderings with feedback, E3 live integrated loop (50 bugs) | final; model and dev-set values pending |
| 6 Expected Outcomes | final |
| 7 Work Plan and Conclusion | final |

Scope (proposal): one benchmark (Defects4J, Java), one model, a conversational
loop with test feedback only. Evaluation at two levels: E1 simulation on
mutation-made candidates (common random numbers across orderings), E2/E3 live
with real feedback (shared response and audit caches). The record as feedback,
agentic loops and imperfect fault localisation are future work. The proposal
reports no preliminary results.

Pending (`\pend`): the model (version, temperature), and the values set on the
development set — variances, ARC location-prior weights, M3 defaults (r, budget B),
margin, eligibility funnel.

## Layout

```
main.tex          preamble (copied from ../main.tex) and chapter order
metadata.tex      cover-page details and working title  <-- EDIT THIS
refs.bib          ../refs.bib carried over + new entries for the taxonomy
frontmatter/      title page, abstract
chapters/         01-introduction  02-background  03-related-works  04-system
                  06-experiments (Evaluation Plan)  07-outcomes  08-plan
figures/          figures, as PDF (an optional bk-logo.pdf goes here)
```

`\pend`, `\pendlong`, `\pendnote{...}` mark what is still outstanding; search
for `\pend` to find them.
