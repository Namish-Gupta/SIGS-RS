# spikes/

Throwaway scripts for learning the stack. Nothing here is imported by `src/`
and nothing here needs to be clean, tested, or reviewed.

The point of a spike is to answer one question fast and then be deleted or
ignored. Keeping them out of `src/` means the real pipeline never accumulates
half-finished experiments.

Useful first spikes for this project:

- `spike_treesitter.py` — **run this first.** Construct the `Language` object
  from `tree_sitter_java`, then parse one Java file and print every method name
  and line range. Answers two things: does the parser install on Windows, and do
  the core and grammar versions agree on ABI? If `Language(...)` raises, that is
  a version mismatch — downgrade `tree-sitter` to 0.23.x and retry before
  reaching for the fallback.
- `spike_embed.py` — embed two sentences, print their cosine similarity.
  Answers: does the model download and cache, and does it run offline afterwards?
- `spike_split.py` — try identifier splitting on twenty real names pulled from
  eTour. Answers: does the highest-leverage trick in the project actually work
  on this corpus?

Run the third one before writing any retrieval code. If identifier splitting
does not produce sensible English on eTour's identifiers, the whole approach
needs rethinking, and it is much cheaper to learn that now.
