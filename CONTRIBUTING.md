# Contributing

Thanks for your interest! A few things to know:

- **This repo is generated from a private source repository** via an export
  script that is not shipped here. It is therefore **not round-trippable**: you
  can't regenerate the repo locally, and a merged pull request may be reworked
  and re-applied upstream rather than landing verbatim. There is no guarantee a
  PR will be merged.
- For anything non-trivial, **please open an issue first** to discuss.
- There is **no editor** in this project — it is a Typst template + data model.
- Several rendering features are **template-dependent**; see the "flags honored
  by modern" table in the README and the "V1 SIMPLIFICATIONS" block in
  `templates/modern/render.typ` before filing a "flag does nothing" issue.

Run `python3 scripts/validate_cv.py` and `./build.sh` before submitting.
