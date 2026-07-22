This is a draft for a list of guidelines that I think are important in a forensics tool and that I try to implement in this fork of Snapchat_Auto.

# To sort...
- Clearly show where all data comes from (file, offset, database coordinates with precise table/column names, text file line number, etc.)
- Allow the user to get an explanation of how a relation was made or how an artifact was decrypted/recovered. The user should be able to manually recreate what the tool does.
- Show both interpreted and raw values or at least allow the user to see raw values with a mouseover or other method.
- When supporting an app, make sure most of the valuable data is shown to the user and don't over-simplify or avoid doing the work to properly decipher what can be realistically obtained. Show the user if something is not available because of missing encryption keys, missing files, 0-byte files, etc.
- The tool should be tested on as many data sources as possible and bugs properly handled and fixed in a timely manner. More specifically, we should make sure to validate compatibility with a range of...
    - Device OS versions
    - App versions
    - Device extraction tools
- The tool UI should be intuitive, _bug-free_ and use proper standard shortcuts like CTRL-W to close a tab, etc.

# Hashing

# Reporting

-dfjsim @2026-07-22
