# SIGS-RS Final Academic Project Report

**Course:** BCSE301L — Software Engineering  
**Degree:** B. Tech. Computer Science and Engineering, VIT Chennai  
**Document Title:** *Secure IoT Gateway Simulation for Resource Sensing (SIGS-RS)*  
**Student:** Namish Gupta (Register No.: 24BCE1934)  
**Project Type:** Individual Project (Team Size: 1)  
**Date:** October 2026  

---

## 1. Document Overview

The final academic project report for SIGS-RS is maintained as a **single, authoritative standalone LaTeX document**:
- **`report/main.tex`**: Master LaTeX source containing all front matter (VIT Title Page, Declaration, Bonafide Certificate with Course Faculty signature block, Acknowledgements, Table of Contents, 1-page Abstract), all 14 technical sections, comprehensive security & performance analyses, inlined TikZ diagrams, full references, and Appendix: Implementation.

> [!IMPORTANT]
> `report/main.tex` has **zero dependencies** on external `.tex` section files (no `\input` or `\include`). Per course requirements, the report ends after the Implementation Appendix; `docs/SRS.pdf` is not appended, and no Appendix B is included.

---

## 2. External Assets Required at Compilation Time

To compile `report/main.tex`, only two local binary image assets are referenced via relative paths:

1. **`vit_logo.png`** (located at `report/vit_logo.png`): High-resolution institutional emblem for the title page.
2. **`figures/use_case_diagram.png`** (located at `report/figures/use_case_diagram.png`): UML Use Case diagram embedded in Section 11.

Both assets are verified and present in the repository.

---

## 3. Formatting Specifications

- **Paper Size:** A4 paper (`\documentclass[a4paper]{article}`, `\usepackage[a4paper,...]{geometry}`).
- **Body Font Size:** 13 pt Times New Roman (`\usepackage[fontsize=13pt]{fontsize}`, `fontspec` with `Times New Roman` and `TeX Gyre Termes` fallback).
- **Code Wrapping:** `fvextra` with `breaklines=true`, `breakanywhere=true`, and scaled monospace font (`DejaVu Sans Mono`, Scale=0.88) ensuring zero horizontal overflow and no clipped code.
- **Section Structure:** Conforms exactly to the 18-part BCSE301L report order (Title Page through Appendix: Implementation).

---

## 4. Compilation Instructions

The report requires **XeLaTeX** or **LuaLaTeX** to load system OpenType fonts.

### Method A: Direct XeLaTeX (Recommended)
From within the `report/` directory, run:
```bash
cd report/
xelatex main.tex
xelatex main.tex
```
*(Running twice is required to resolve cross-references, table numbering, and Table of Contents page numbers).*

### Method B: Using `latexmk`
```bash
cd report/
latexmk -xelatex main.tex
```

### Method C: LuaLaTeX
```bash
cd report/
lualatex main.tex
lualatex main.tex
```

### Method D: Overleaf / Cloud LaTeX
1. Create a zip archive containing:
   - `report/main.tex`
   - `report/vit_logo.png`
   - `report/figures/` (including `use_case_diagram.png`)
2. Upload the archive as a new project in Overleaf.
3. In Overleaf Settings, set **Compiler** to **XeLaTeX**.
4. Set **Main document** to `main.tex` and click **Recompile**.
