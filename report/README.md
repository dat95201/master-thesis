# Thesis proposal

*Cutting the Cost of LLM-Based Program Repair with Evidence-Driven Model
Escalation* (CO5109). The built document is `main.pdf`.

## Build

```bash
make            # latexmk -pdf; needs a TeX distribution with latexmk and bibtex
make clean      # removes build files, keeps main.pdf
```

Without `make`: `latexmk -pdf main.tex`.

## Files

```
main.tex              packages, page style, macros; includes everything below
metadata.tex          title, author, instructor, course, date
frontmatter/          title page, abstract
chapters/
  01-introduction     problem, goal, research questions, contributions, scope
  02-background       cost model, break-even of escalation, verified progress
  03-related-works    repair systems, cost measurement, model selection, gaps
  04-system           the four modules M0 to M3
  05-evaluation       bugs, policies, experiments E0 to E4, metrics, statistics
  06-outcomes         how each possible outcome will be read
  07-plan             work plan, risks, threats to validity, conclusion
refs.bib              the cited works only
venue-year.bst        natbib's unsrtnat with one change: an entry without a
                      `year` prints no date (conference entries carry the year
                      in the venue name, e.g. "ISSTA 2015")
figures/              the university logo; the diagrams are TikZ, in the chapters
```

Numbers that the code depends on (budgets, prices, thresholds) are stated in
Chapter 5 and mirrored in `../settings.json`. Change both together.
