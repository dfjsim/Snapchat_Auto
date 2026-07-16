# pandas 3.x / Python 3.14 compatibility notes

Running the tool under **pandas 3.0.3** on **Python 3.14** surfaces a class of failures that
did not occur on the older pandas (1.x/2.x) this project was originally written against. The
parsing scripts do a lot of per-cell DataFrame mutation (`df.loc[i, col] = value`) on frames
built from SQL and merges, which is exactly where the new, stricter behaviour bites.

This doc records the incompatibilities, the concrete sites that were fixed, and the pattern to
follow when editing these scripts.

## 1. Strict dtype enforcement on cell assignment

**pandas 3.x refuses to store a value whose type doesn't match the column's dtype.** Older
pandas silently upcast the column (e.g. `float64` → `object`); pandas 3.x raises instead:

```
TypeError: Invalid value 'Temporarily stored media' for dtype 'float64'
TypeError: Invalid value 'local_message_reference' for dtype 'int64'
TypeError: Invalid value 'None' for dtype 'float64'
```

A column becomes numeric (and therefore vulnerable to a later string assignment) whenever:

- it is **all-`NaN`** after a `merge`/`concat`/`reindex` (pandas infers `float64`);
- it comes from a **SQL column that is entirely non-null integers** (inferred `int64`);
- it was just run through **`pd.to_numeric(...)`**.

### Fix pattern

Widen the target column to `object` **before** writing strings into it:

```python
if 'content_type' in df.columns:
    df['content_type'] = df['content_type'].astype(object)
```

This reproduces the old silent-upcast behaviour exactly, so downstream logic is unchanged.

### Sites fixed (`scripts/ParseSnapchat_iOS.py`)

| Location | Column | Why it was numeric | Fix |
|----------|--------|--------------------|-----|
| `mergeCacheChats` string-labelling loop | `merge_df['content_type']`, `message_content`, `server_message_id` | all-`NaN` after the merge/concat | cast the three columns to `object` after `reset_index`, before the loop |
| `mergeCacheChats` cache/arroyo match loops | `chats_df['content_type']` | `int64` straight from `conversation_message.content_type` | cast to `object` at the top of the function |
| `mergeCacheChats` sending-messages loop | `merge_df['server_message_id']` | just made numeric by `pd.to_numeric(...)` | cast to `object` when `sending_messages` is non-empty, before assigning `"None"` |

> **Note:** creating a *brand-new* column via a partial assignment
> (`df.loc[i, "NewCol"] = "str"`) is still fine — pandas creates it as `object`. Only writing
> into an *existing* numeric column raises. Verified for the `LINK`/`KEY`/`IV` columns in
> `getContentmanager`, which need no change.

### When editing these scripts

Any new `df.loc[i, col] = <non-numeric>` on a column that could be all-null or numeric needs
the same `astype(object)` guard. The failure only appears on data that actually exercises the
branch, so it can pass on one extraction and fail on the next.

## 2. `DataFrame.append()` was removed in pandas 2.0

`some_df.append(row)` no longer exists and raises `AttributeError`. Replace with `concat`:

```python
# before
group_df = group_df.append(row)
# after
group_df = pd.concat([group_df, row.to_frame().T], ignore_index=True)
```

Fixed in `getFriendsAppGroupPlistStorage` (`scripts/ParseSnapchat_iOS.py`). It had been masked
by a surrounding `try/except`, so instead of crashing it silently dropped every no-name group
and logged an error per row.

## 3. Unrelated but fixed in the same pass

- **`SyntaxWarning: invalid escape sequence "\."`** — `re.compile("^\.+$")` in
  `ParseSnapchat_iOS.py` and `getCacheAndroid.py`. Use a raw string: `re.compile(r"^\.+$")`.
- **`logging` misuse** — `logger.info(f"msg", E)` passes `E` as a `%`-format argument to a
  message with no placeholders, raising `TypeError: not all arguments converted during string
  formatting` and burying the real error under a `--- Logging error ---` trace. Use
  `logger.info(f"msg: {E}")`.
- **`UnboundLocalError` on `grupper`** — a dict initialised *inside* a `try` after the statement
  that can throw, then used unconditionally afterwards. Initialise before the `try`.
- **`mode=ro` on a database that must be created** — in
  `scripts/DecryptLocalMemories_iOS.py`, the SQLCipher recovery path deleted the target file and
  reopened it with `?mode=ro`, so `executescript` hit a `None` connection
  (`unable to open database file` → `'NoneType' object has no attribute 'executescript'`). Use
  `?mode=rwc` when the connection must create/populate the file.

## 4. Schema drift (not a pandas issue, but seen in the same run)

Newer Snapchat iOS builds (observed on iOS 26.2.1) no longer have some tables/paths the scripts
expect. These are handled as expected-empty rather than errors:

- `arroyo` has **no `user_conversation` table** — the optional "groups with no name"
  enrichment is skipped.
- **no `contentmanagerV3_*` / `CONTENT_OBJECT_TABLE`** — `getContentmanager` returns an empty,
  correctly-shaped frame when the database is absent instead of querying a missing table.
