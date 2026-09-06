# Search: management workflow and legacy boundary

Search is part of **management**, not a separate public Skill capability. It has two explicit modes:

```text
management search "<album-name-fragment>" --mode metadata --target albums
management search "<filename-fragment>" --mode metadata --target photos
management search "<Chinese or English visual query>" --mode semantic --profile-id <profile-id>
```

Prefix commands with `python <skill-directory>\scripts\photography.py --state-dir <state-directory>`. See [management](management.md) for exact scopes, pagination, result fields and read-only HTML/JSON exports.

Metadata mode uses Unicode NFC/casefold literal matching on album names or photo filenames/relative paths. It needs neither a model nor a default profile.

Semantic mode searches **image embeddings**, not saved descriptions. Use [index](index.md) for the optional `requirements-index.txt` runtime, authorized `index setup`, explicit profile selection, confirmed indexing and recovery. First setup does not choose a default; configure explicitly or pass a profile ID. Image/text encoders must match the same profile.

Ordinary search is offline, encodes only the query, reports incomplete coverage and never creates missing photo vectors, changes an album/default, downloads weights or calls cloud analysis. Scores are relative cosine similarities, not probabilities or guaranteed matches. Real-model quality has not been established by synthetic tests.

## Compatibility-only saved selections

The old description-embedding runtime, text recipe and installation/generation/search entry points have been retired. Do not reinstall that runtime or use its old workflow to prepare the new image index. Legacy description-vector rows remain in SQLite for preservation, not as fallback candidates for a different model space.

The existing legacy search-report renderer and `search-add` remain available only for already supported **legacy search snapshots** and explicitly requested compatibility use. They do not accept the new `management-snapshot-v1` format.

In that old protocol, selection adds only explicit photo IDs present in the saved snapshot, without rerunning the query or copying vectors/observations. Old report checkboxes belong only to that compatibility protocol. They are not new management widgets, and a new management search must not automatically direct the user to save a selection into an album.

New HTML reports are read-only snapshots with no selection controls, album writes or live search server. If an operation is outside the three current capabilities, explain that boundary instead of silently substituting a legacy workflow.
