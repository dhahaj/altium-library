# Altium DbLib Server (Celestial, but yours)

A self-hosted SQL Server that holds a full copy of the Celestial Altium Library **plus your own parts**.
Altium connects with a read-only login. You edit parts with a separate read/write login.

```
Celestial Azure SQL ──(read-only, one-shot copy)──► your SQL Server ◄── Altium (altium_reader)
                                                     │  dbo.<Celestial tables>  ← replaced on each mirror
                                                     │  dbo.[Custom - ...]      ← yours, never touched
                                                     ├── Parts Manager web UI (librarian)
                                                     └── DBeaver/SSMS (librarian)
```

Note: the upstream git repo no longer ships a SQL dump. The `.DbLib` just says *"Download from portal.altiumlibrary.com"*. So this kit copies the data out of the hosted database using your own portal credentials.

---

## 1. Server (Proxmox)

**Host:** a Debian VM, or an LXC with Docker (`features: nesting=1,keyctl=1`). Give it 2+ vCPU, 3–4 GB RAM and 20 GB disk. SQL Server needs a 2 GB minimum, and the default cap here is 2 GB.

```bash
# on the Docker host
git clone <wherever you keep this> /opt/altium-dblib-server   # or copy the folder over
cd /opt/altium-dblib-server
cp .env.example .env && chmod 600 .env
nano .env            # passwords, BIND_ADDR, Celestial credentials, PARTSUI_PASSWORD
./setup.sh all       # up + init + mirror + ui
```

Individual steps:

| Command | What it does |
|---|---|
| `./setup.sh up` | Start SQL Server and wait until it's healthy |
| `./setup.sh init` | Create the DB, `altium_reader` + `librarian` logins, a `Custom - Template` table, the `make_custom_table` proc and the Parts Manager's search index. Safe to re-run (it also re-syncs passwords from `.env`) |
| `./setup.sh mirror --list` | Test the Celestial connection: list tables and row counts, copy nothing |
| `./setup.sh mirror` | Copy all Celestial tables in. Re-run whenever you want upstream updates |
| `./setup.sh mirror --only "Capacitors"` | Re-copy only matching tables (e.g. after a failure) |
| `./setup.sh backup` | Full backup to `./backup/`, keeps the newest 10 |
| `./setup.sh sql` | sqlcmd prompt as `sa` |
| `./setup.sh ui` | Build and start the Parts Manager web UI (section 4). Needs `PARTSUI_PASSWORD` |
| `./setup.sh all` | `up` + `init` + `mirror` + `ui` (skips `ui` with a note if `PARTSUI_PASSWORD` is empty) |

**Nightly backup:** `crontab -e` → `15 3 * * * /opt/altium-dblib-server/setup.sh backup >/dev/null 2>&1`

### Celestial credentials
Log in at portal.altiumlibrary.com and grab the server, database, user and password from the connection string or UDL it gives you. Put them in `.env`.
Its Azure firewall rule must allow the **public IP of the box running the mirror**. If you're at home, that's the same IP as your desktop. A firewall rejection prints a specific message, so you'll know.

The mirror loads each table into a `staging` schema, then swaps it into `dbo` in one transaction. A dropped connection therefore never leaves a table half-empty. Any table starting with `Custom - ` is skipped, in both the source and your copy.

### Network
- Set `BIND_ADDR` to the host's LAN or Tailscale IP, and/or restrict 1433 to your LAN subnet in the Proxmox firewall.
- **Don't** route 1433 through Nginx Proxy Manager or open it on the router. SQL Server on the open internet gets brute-forced within hours.
- Traffic is TLS-encrypted with SQL Server's self-signed cert, which is why the client strings below trust it.

---

## 2. Altium (Windows)

1. **Install the driver:** Microsoft OLE DB Driver 19 for SQL Server (`MSOLEDBSQL19`).
2. **Get the library files.** Symbols, footprints and STEP models stay file-based:
   `git clone https://github.com/issus/altium-library.git C:\Altium\celestial`
   This is big; expect a long first clone. `git pull` updates it later.
3. **Make your own DbLib.** Copy `C:\Altium\celestial\Altium Database Library V2.DbLib` to `C:\Altium\Daniel.DbLib`. Keeping it outside the repo means `git pull` never overwrites your settings.
4. Open `Daniel.DbLib` in Altium → **Connection** → *Use Connection String* → **Build** or paste:
   ```
   Provider=MSOLEDBSQL19;Data Source=192.168.1.50,1433;Initial Catalog=altium_library;User ID=altium_reader;Password=YOUR_READER_PASSWORD;Use Encryption for Data=Mandatory;Trust Server Certificate=True;
   ```
   Click **Connect**. The 323 Celestial tables should go green.
5. **Tools ▸ Options ▸ Symbol & Model Search Paths:** add `C:\Altium\celestial` (tick *Include Sub-Directories*) and your own library folder, e.g. `C:\Altium\daniel-libs`.
6. Save, then **Components panel ▸ ≡ ▸ File-based Libraries Preferences ▸ Installed ▸ Install** → pick `Daniel.DbLib`.

---

## 3. Adding your own parts

The quick way is the **Parts Manager** web UI (section 4): search, clone, save. Everything below is the SQL it does for you, and still works if you prefer DBeaver.

**One-time per category.** In DBeaver or SSMS, connect as `librarian` and run:
```sql
-- Same columns as a Celestial table (handy for passives with Capacitance/Voltage/etc.)
EXEC dbo.make_custom_table @like = N'Capacitors - Ceramic';   -- -> [Custom - Capacitors - Ceramic]

-- Or the generic template columns
EXEC dbo.make_custom_table @name = N'Connectors - Detex';     -- -> [Custom - Connectors - Detex]
```
Then in Altium, open `Daniel.DbLib` and reconnect. Tick **Enable** on the new table. Set its key to **Single key lookup → `Part Number`**, or the where-clause `[Part Number] = '{Part Number}'` to match the Celestial tables. Then check the field mappings (e.g. make `Comment` visible) and save.

**Each new part:**
1. Draw the symbol in `C:\Altium\daniel-libs\Daniel.SchLib` and the footprint in `Daniel.PcbLib`. Put that folder in its own git repo.
2. Insert a row:
   ```sql
   INSERT INTO [Custom - Connectors - Detex]
     ([Part Number], [Comment], [Description], [Library Ref], [Library Path], [Footprint Ref], [Footprint Path],
      [Manufacturer 1], [Manufacturer Part Number 1])
   VALUES
     ('DTX-J-0001', 'Molex 2x4', 'Micro-Fit 3.0 2x4 RA header', 'CONN_2X4', 'Daniel.SchLib', 'MOLEX_43045-0800', 'Daniel.PcbLib',
      'Molex', '43045-0800');
   ```
   A bare filename in the path columns works because Altium resolves it through the search paths from step 5.
3. In the Components panel, hit refresh. The part is there.

**Schema changes** (adding columns to a Custom table) need `sa` (`./setup.sh sql`). `librarian` can only read/write rows and create tables via the proc. That's deliberate.

**Never edit rows in the Celestial tables.** The next mirror replaces them. If a Celestial part is wrong, copy it into a Custom table under the same Part Number and disable or ignore the original, or report it upstream.

---

## 4. Parts Manager (web UI)

A small web app on the Docker host for adding parts without SQL:

> search for a similar part → **Clone** → change value / part number / manufacturer → **Save** → refresh Altium's Components panel.

It logs in to SQL Server as `librarian` and only ever writes to tables whose name starts with `CUSTOM_PREFIX`. Celestial tables are read-only to it, so the next mirror can't wipe your work.

### First-time setup

1. **Set the password** in `.env` (the login is user `admin`):
   ```bash
   PARTSUI_PASSWORD=pick-something-long
   PARTSUI_PORT=8080                 # optional
   #PARTSUI_BIND_ADDR=192.168.1.50   # optional, defaults to BIND_ADDR
   ```
2. **Mount your libraries (optional, for autocomplete).** The container sees `./libs` as `/libs` (read-only) and scans `/libs/celestial` and `/libs/daniel` for `.SchLib` / `.PcbLib`. Without them, the symbol and footprint fields are plain text boxes.
   ```bash
   cd /opt/altium-dblib-server
   # Celestial symbols + footprints only (skips ~18k STEP models)
   git clone --filter=blob:none --sparse https://github.com/issus/altium-library.git libs/celestial
   git -C libs/celestial sparse-checkout set symbols footprints
   # your own symbol/footprint repo
   git clone <your libs repo> libs/daniel
   ```
   Different folders? Set `PARTSUI_LIBS_DIRS` (colon-separated paths *inside the container*, under `/libs`).
3. **Create the database objects and start it:**
   ```bash
   ./setup.sh init   # adds sql/02_partsui.sql: search index + its procs. Safe on an existing server
   ./setup.sh ui     # build, start, wait until healthy, print the URL
   ```
   Open `http://<host>:8080/` and log in as `admin`.

Upgrading an existing server is the same: `git pull`, then `./setup.sh init` and `./setup.sh ui`. Init builds the search index from the tables you already have.

### Daily workflow

1. **Search.** Type 2+ characters: part number, comment or description. Every word must match somewhere, and part numbers that *start* with what you typed come first. Filter by category or tick **Mine only**.
2. **Clone.** Open a similar part and click **Clone to my library**. The form is pre-filled from that part, with Part Number cleared (the old one is shown as a hint). It saves to `[Custom - <same category>]`.
3. **Edit and save.** The seven fields Altium needs are pinned at the top and required. In *Library Ref* / *Footprint Ref*, start typing a symbol or footprint name and pick a suggestion: it fills the matching *Path* too (e.g. `Daniel.SchLib`, or `footprints/…/X.PcbLib` for Celestial). Numbers, dates and lengths are checked against the column types before anything is written.
4. **Refresh Altium.** Components panel ▸ refresh. If the save created a new table, you'll see *"New table created … enable it in your DbLib"*; do the DbLib step below once for that table.

Also:
- **New part** (top menu): start blank in one of your tables, a copy of a Celestial category's columns, or a brand-new table name.
- **Edit / Delete** appear on your own parts only. You can change a part's Part Number when editing.
- **Duplicates:** a Part Number that's already in one of your tables is refused. One that's in Celestial is refused unless you tick *Allow a duplicate of a Celestial part number* (e.g. to override a wrong Celestial entry).
- The new table isn't created until you **save**, so backing out of a clone leaves nothing behind.

### DbLib page: make new tables visible in Altium

Altium only shows tables that are listed and enabled in your `.DbLib`. The **DbLib** page does that edit for you:

1. Upload your current `Daniel.DbLib` and click **Check**. It lists each of your `Custom - …` tables as *Enabled*, *Present but disabled* or *Missing*.
2. Click **Download patched copy**. Missing tables are appended as new `[TableN]` + `[FieldMapN]` sections, enabled, keyed on `[Part Number] = '{Part Number}'`, with field mappings in the same pattern as Celestial's tables (`Comment`/`Value` visible on placement). Nothing else in the file changes, byte for byte, line endings included.
3. Keep a backup of the old file, close Altium (or uninstall the library), save the download over `Daniel.DbLib`, and reopen.

The file is read in memory and never stored or logged: it holds your reader password. A table that's present but *disabled* isn't touched; tick **Enable** for it in Altium yourself.

### Status page

DB connection, part counts, last mirror run and scanned library files. Two buttons:
- **Re-index search**: rebuilds the search index from every table. The mirror and the app keep it current, so you only need this after adding or editing rows in DBeaver.
- **Rescan libraries**: after a `git pull` in `./libs/…`.

### Troubleshooting

- **`altium-partsui` keeps restarting:** `docker compose logs partsui`. *"PARTSUI_PASSWORD is empty"* means set it in `.env` and run `./setup.sh ui` again.
- **Footer says *Database not connected*:** SQL Server is down, or `LIBRARIAN_PASSWORD` in `.env` doesn't match the login. Run `./setup.sh init` to re-sync it, then `docker compose restart partsui`.
- **Search doesn't find a part you added in DBeaver:** Status ▸ **Re-index search**.
- **No symbol/footprint suggestions:** Status shows how many library files were found. Check the folders exist under `./libs` on the host, then **Rescan libraries**. Files that can't be read are listed there and skipped.
- **"Form expired":** the page was open from before a restart or the browser dropped the cookie. Reload and submit again.
- **Saved, but Altium doesn't show the part:** the table isn't enabled in your `.DbLib` yet (DbLib page), or the Components panel needs a refresh.
- **"Not allowed" page:** by design, the app writes only to `Custom - …` tables. Celestial tables are read-only here.

### Security

- HTTP Basic auth over plain HTTP. Keep it on your LAN or Tailscale like port 1433; don't publish it through Nginx Proxy Manager or the router.
- Every form post carries a CSRF token, and pages are sent `no-store` with a strict Content-Security-Policy.
- It uses the `librarian` login, never `sa`. Table and column names come only from the database catalog; values always go in as query parameters.

### Running the tests

```bash
cd partsui
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```
Needs Microsoft ODBC Driver 18 on the machine and Docker: the integration tests start a throwaway `mssql/server:2022-latest` container and remove it afterwards. To use an existing **throwaway** server instead, set `PARTSUI_TEST_MSSQL=host,port`, `PARTSUI_TEST_SA_PASSWORD=…` and `PARTSUI_TEST_MSSQL_THROWAWAY=yes`. The tests drop and recreate `altium_library` and its logins there, so never point it at the real one.

---

## Files

```
docker-compose.yml   SQL Server 2022 (Express) + one-shot mirror tool
.env.example         all settings; copy to .env
setup.sh             up / init / mirror / backup / sql / ui / all
sql/01_init.sql      DB, logins, permissions, Custom template, make_custom_table proc
sql/02_partsui.sql   Parts Manager search index (_parts_index) + refresh/upsert procs
mirror/mirror.py     Celestial -> local copy (staging + atomic swap), then refreshes the index
mirror/Dockerfile    python 3.12 + msodbcsql18 + pyodbc
partsui/             Parts Manager: FastAPI + Jinja2 + vendored htmx, Dockerfile, tests
libs/                (not in git) optional library clones mounted read-only into partsui
```

## Troubleshooting

- **`Password validation failed`** during init: SQL Server rejects passwords that contain the login name or are too simple. Change the password in `.env` and re-run `init`.
- **Mirror `Login timeout` / `40615`:** this host's public IP isn't in the Celestial portal firewall.
- **Altium "Data source name not found" or provider errors:** the OLE DB driver isn't installed, or the `Provider=` name doesn't match the driver version (`MSOLEDBSQL19` vs `MSOLEDBSQL`).
- **Data dir permission errors on first `up`:** SQL Server runs as uid 10001. `setup.sh up` chowns `./data`, but on an unprivileged LXC with a bind-mounted dataset you may need to map that uid.

## Ideas

Not built yet; candidates for later:

- DigiKey / Mouser / Octopart lookup to fill parameters from a manufacturer part number (needs API keys).
- Editing symbols or footprints in the browser. For now that stays in Altium.
- Multiple user accounts and an audit log of who changed which part.
- Writing the DbLib straight onto the Windows PC (a small agent there, or a shared folder) instead of download-and-replace.
- Show a rendered preview of the chosen symbol and footprint next to the form.
