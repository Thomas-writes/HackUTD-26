# json File I/O Summary
## jsonFile.ts: browser JSON helpers
#### `readJsonFile$(file)`:
Reads a selected `File` and parses it as JSON and emits the result once.
`defer` means nothing happens until someone subscribes.
A read or parse failure comes through the observable's error channel.
<br>
<br>

#### `readJsonUrl$(url)`:
Fetches JSON from a URL using `fromFetch`, which cancels the request on unsubscribe.
It does not check `res.ok`, so a 404 with an HTML body will fail at the parse step rather than with a clear HTTP error.
<br>
<br>

#### `downloadJson$(filename, data)`:
Pretty-prints the data, wraps it in a blob, and clicks a temporary `<a download>` link to start the download.
It then revokes the object URL, emits once and completes.
<br>
<br>
<br>
<br>

## spacetime.ts: SpacetimeDB connection and live data
#### `connect$(uri, dbName, token?)`:
Builds a `DbConnection`, subscribes to `SELECT * FROM person`, and emits the connection once the server confirms the subscription.
At that point the local cache is populated.
Connection errors are forwarded to the Observable.
<br>
<br>

#### `PersonRow`:
The plain `{ name }` shape for a row.
<br>
<br>

#### `people$(conn)`:
Emits the current rows immediately and a fresh full snapshot after every insert or delete.
Its teardown removes the listeners so nothing leaks.
<br>
<br>
<br>
<br>

## sync.ts: Ties the two files together
#### `importPeople$(file, add)`:
Reads the file, flattens the array into single rows, and drops any row without a non-blank string `name`.
It then calls the `add` reducer for each row, one at a time and in file order, and finally emits how many rows were imported.
<br>
<br>

#### `exportPeople$(rows, filename)`:
Strips each row down to `{ name }` and triggeres a download through `downloadJson$`.