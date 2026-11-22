# Altium DbLib Server (Celestial, but yours)

A self-hosted SQL Server that holds a full copy of the Celestial Altium Library **plus your own parts**.
Altium connects with a read-only login. You edit parts with a separate read/write login.

```
Celestial Azure SQL ──(read-only, one-shot copy)──► your SQL Server ◄── Altium (altium_reader)
                                                     │  dbo.<Celestial tables>  ← replaced on each mirror
                                                     │  dbo.[Custom - ...]      ← yours, never touched
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
nano .env            # passwords, BIND_ADDR, Celestial credentials
./setup.sh all       # up + init + mirror
```

Individual steps:

| Command | What it does |
|---|---|
| `./setup.sh up` | Start SQL Server and wait until it's healthy |
| `./setup.sh init` | Create the DB, `altium_reader` + `librarian` logins, a `Custom - Template` table and the `make_custom_table` proc. Safe to re-run (it also re-syncs passwords from `.env`) |
| `./setup.sh mirror --list` | Test the Celestial connection: list tables and row counts, copy nothing |
| `./setup.sh mirror` | Copy all Celestial tables in. Re-run whenever you want upstream updates |
| `./setup.sh mirror --only "Capacitors"` | Re-copy only matching tables (e.g. after a failure) |
| `./setup.sh backup` | Full backup to `./backup/`, keeps the newest 10 |
| `./setup.sh sql` | sqlcmd prompt as `sa` |

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

## Files

```
docker-compose.yml   SQL Server 2022 (Express) + one-shot mirror tool
.env.example         all settings; copy to .env
setup.sh             up / init / mirror / backup / sql / all
sql/01_init.sql      DB, logins, permissions, Custom template, make_custom_table proc
mirror/mirror.py     Celestial -> local copy (staging + atomic swap)
mirror/Dockerfile    python 3.12 + msodbcsql18 + pyodbc
```

## Troubleshooting

- **`Password validation failed`** during init: SQL Server rejects passwords that contain the login name or are too simple. Change the password in `.env` and re-run `init`.
- **Mirror `Login timeout` / `40615`:** this host's public IP isn't in the Celestial portal firewall.
- **Altium "Data source name not found" or provider errors:** the OLE DB driver isn't installed, or the `Provider=` name doesn't match the driver version (`MSOLEDBSQL19` vs `MSOLEDBSQL`).
- **Data dir permission errors on first `up`:** SQL Server runs as uid 10001. `setup.sh up` chowns `./data`, but on an unprivileged LXC with a bind-mounted dataset you may need to map that uid.
