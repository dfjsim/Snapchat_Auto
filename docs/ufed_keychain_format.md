# The UFED keychain export (`backup_keychain_v2.plist`)

Cellebrite UFED exports an iPhone's keychain as a binary property list, `backup_keychain_v2.plist`,
next to a full-filesystem extraction. The items in it are still encrypted the way iOS stores them;
what UFED adds is the keys iOS would otherwise only release on the device. This note describes the
file as it is, so that it can be read without any other tool. It is implemented by
`scripts/data/ufed_keychain.py`.

Sources: Apple's *Platform Security Guide* (keychain data protection: every item is encrypted with
AES-256-GCM under a per-item key, its attributes and its secret separately, the attributes' key
wrapped by a class key), ITU-T X.690 for DER, and the exports themselves — every statement below was
checked by decrypting every item of the UFED exports in the test corpus, with the GCM tag verifying.

## The envelope

```
{
  "classKeyIdxToUnwrappedMetadataClassKey": { "<class index>": <32 bytes>, … },
  "keychainEntries": [
    {
      "table":       "genp" | "inet" | "keys" | …,      the keychain table the row came from
      "rowID":       <int>,                             its row in that table
      "version":     <int>,                             the item format (8 in the corpus)
      "classKeyIdx": <int>,                             which metadata class key wraps it
      "metadata": { "wrappedKey": <archive>, "ciphertext": <archive>, "tamperCheck": <UUID string> },
      "data":     { "unwrappedKey": <32 bytes>, "ciphertext": <archive>, "tamperCheck": <UUID string> }
    }, …
  ]
}
```

Each `<archive>` is an `NSKeyedArchiver` binary plist whose root object is an
`_SFAuthenticatedCiphertext` with three byte strings: `SFCiphertext`, `SFAuthenticationCode` (16
bytes, the GCM tag) and `SFInitializationVector` (32 bytes, the GCM nonce).

## Decrypting one item

All three steps are **AES-256-GCM** with the archive's IV as nonce and its authentication code as tag,
and no associated data; the tag verifies on every item, so a wrong key is detected rather than
producing garbage.

1. **The item's metadata key** = decrypt `metadata.wrappedKey` with
   `classKeyIdxToUnwrappedMetadataClassKey[str(classKeyIdx)]` → 32 bytes.
2. **The attributes** = decrypt `metadata.ciphertext` with that metadata key.
3. **The secret** = decrypt `data.ciphertext` with `data.unwrappedKey` (UFED already unwrapped it).

An item whose class key is not in the export, or whose `unwrappedKey` is missing, cannot be fully read:
its attributes are kept when step 2 succeeds, and it simply has no secret.

## The decrypted content

Both plaintexts are a DER-encoded dictionary: a `SET` (`0x31`) of `SEQUENCE`s (`0x30`), each holding a
`UTF8String` key and one value. The secret's DER is followed by padding (n bytes of value n), which is
not part of the dictionary. Values seen:

| DER | tag | read as |
|---|---|---|
| UTF8String / PrintableString / IA5String | `0x0c` / `0x13` / `0x16` | text |
| OCTET STRING | `0x04` | bytes |
| INTEGER | `0x02` | integer |
| BOOLEAN | `0x01` | boolean |
| GeneralizedTime / UTCTime | `0x18` / `0x17` | a UTC date |
| NULL | `0x05` | absent |
| SEQUENCE / SET | `0x30` / `0x31` | a list / a nested dictionary |

Anything else is kept as its raw bytes. The attribute names are iOS's short names — `agrp` (access
group), `acct` (account), `svce` (service), `gena` (generic attribute), `labl`, `cdat` / `mdat`
(created / modified), `pdmn` (protection class), `sync`, `tomb`, `persistref`, `UUID`, `TamperCheck`, …
— and the secret carries `v_Data` (the item's value) and a `TamperCheck`, which equals the entry's
`data.tamperCheck` (and the attributes' `TamperCheck` the entry's `metadata.tamperCheck`). `acct` and `gena` are text on some items and bytes on others.

## What the reader writes

`decrypted_keychain.plist`: a list with one dictionary per item — the attributes, then the secret's
`v_Data`, plus `table`, `rowID` and `classKeyIdx` from the envelope. This is what the Memories reports
search for Snapchat's items (`agrp` `3MY7A92V5W.com.toyopagroup.picaboo`; `acct`/`gena`
`egocipher.key.avoidkeyderivation` and `com.snapchat.keyservice.persistedkey`).
