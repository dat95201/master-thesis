MAIN := main

.PHONY: all clean distclean

all: $(MAIN).pdf

$(MAIN).pdf: $(MAIN).tex metadata.tex refs.bib venue-year.bst \
             $(wildcard chapters/*.tex) $(wildcard frontmatter/*.tex) \
             $(wildcard figures/*)
	latexmk -pdf -interaction=nonstopmode -halt-on-error $(MAIN).tex

# no latexmk? `tectonic main.tex` builds the same PDF.

clean:
	latexmk -c $(MAIN).tex
	rm -f $(MAIN).bbl

distclean: clean
	rm -f $(MAIN).pdf
