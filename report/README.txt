# SIGS-RS Project Report LaTeX Template

This template follows the section order and cover-page information in the supplied
`Project_Report_Format.docx` for BCSE301L Software Engineering.

## Files
- `project_report_template.tex`: editable report skeleton.
- `vit_logo.*`: logo extracted from the supplied Word template, if present.

## Compile
Use XeLaTeX or LuaLaTeX because the template uses `fontspec`.

```bash
xelatex project_report_template.tex
xelatex project_report_template.tex
```

The second run updates the table of contents.

## Before submission
- Replace both team member and register-number placeholders.
- Review the exact font requirement. The template uses Times New Roman if installed;
  otherwise it falls back to TeX Gyre Termes.
- Fill every section with project-specific, original writing and genuine evidence.
- Keep SRS requirements faithful to the authoritative `docs/SRS.pdf`.
- Add genuine demo screenshots only after capturing them.
- Check course instructions about whether the original SRS PDF must also be appended.
