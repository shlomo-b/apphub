# AppHub

AppHub is a small dashboard for the tools you use every day. Sign in, pick a section tab, and open a tile. Each tile is a name, a URL, and an icon you choose.

It follows the same pattern as **CronBoard** and **k8s-IPAM**: FastAPI, Docker Compose, MongoDB Atlas (or local Mongo, or a JSON file), session login, idle logout, and a Mongo status label in the header.

| | |
|--|--|
| **Repo** | [shlomo-b/apphub](https://github.com/shlomo-b/apphub) |
| **App code** | `apphub-ui/` |
| **Image** | `apphub:v1.0.0` |
| **Port** | **8092** |

---

## What the app does

After sign-in you get a personal launcher for DevOps, networking, and cloud tools:

- Group apps into **sections** (tabs)
- Add / edit / remove tiles (name, URL, icon)
- Search within the current section
- Dark mode (stored in the browser)
- Icons uploaded or pasted as URLs, stored in Atlas and served by AppHub

Default sections on first seed:

| Tab | Atlas collection |
|-----|------------------|
| DevOps-Tools | `devops` |
| Networking-Tools | `networking` |
| Clouds | `clouds` |

You can rename or delete any section (including the built-ins), create new ones, and add as many apps as you need. At least one section must remain.

---

## What you see

| Area | What it is |
|------|------------|
| **Login** | Split screen: hub art on the left, form on the right |
| Header | Dark mode, AppHub brand, section picker, search |
| Right side | `Welcome, <user>`, **Log out**, Mongo status under them |
| Grid | One card per app in the current section, plus **Add app** |
| Card | Icon, name, **Edit**, **Remove**. Click the card to open the URL |

### Login screen

Same layout idea as CronBoard / Argo CD (no SSO — AppHub only supports username/password):

- Left: production hub-style image (`static/img/login-dark.png`)
- Right (narrow panel): **AppHub** title at the top, username / password, teal **SIGN IN**, smaller **AppHub** at the bottom
- No tagline under the title
- Fits the viewport (no page scroll behind the footer)

### After login

The section picker shows **Current: &lt;tab&gt;** with every section plus **New section +**. The pencil opens **Edit section** (**Save** renames, **Delete** removes the tab and its apps).

Section and app names are capitalized on save (`resources` → **Resources**, `uptime-kuma` → **Uptime-Kuma**). Capitals you type yourself are kept (`AWS prod` → **AWS Prod**).

Search filters the current tab by name or URL. Switching tabs is instant (in-memory filter, no Atlas round trip).

---

## Apps and icons

Anyone signed in can add, edit, or remove a tile. There is no separate admin role.

**Add app** / **Edit app**:

- **Name** and **URL** (must start with `http://` or `https://`)
- **Icon**: paste an image URL, or upload png / jpg / webp / svg / gif (max 2MB)

The same URL cannot be saved twice in the same section.

Uploaded and downloaded images are resized to fit **256×256** (Pillow) before save, so a large photo becomes a small icon instead of a blown-up crop. Transparent images stay PNG, the rest become JPEG, SVG is kept as-is. Changing the icon URL or uploading a new file replaces the previous image (cache cleared).

Custom icons live in Atlas `icons` (bytes + original URL if pasted). Tiles always load icons from AppHub (`/api/icons/...`), so hotlinked sites cannot hide the picture.

Older tiles that still point at the built-in Grafana logo use `/static/img/grafana.png` until you set your own icon. New apps expect a custom icon (URL or upload).

---

## Login and session

Login uses `APPHUB_USER` / `APPHUB_PASSWORD` (compose defaults: `shlomo` / `shlomo`).

The session is a cookie (`SameSite=Lax`). The signing secret lives in Atlas `settings` (`_id: session`). On file mode it is `data/.session_secret`.

If nobody uses the page for **3 minutes**, a **30 second** warning appears (**Stay signed in**). Mouse, keyboard, scroll, or that button resets the timer. The login screen does not flash on refresh: it stays hidden until `/api/me` answers.

The header shows **mongodb-atlas: Connected** when Atlas is up, or **Disconnected** (and `mongodb: Connected` for local Mongo).

---

## Quick start

From the app folder:

```bash
cd apphub-ui
docker compose up -d --build
```

Open http://localhost:8092 and sign in.

```bash
docker compose down
```

After UI changes, rebuild and hard-refresh the browser so cached CSS/JS/images are not used.

---

## Repository layout

```
.
├── README.md                 # this file
├── .github/workflows/        # CI (Docker image)
└── apphub-ui/                # runnable AppHub app
    ├── app/
    │   ├── main.py           # API, icons, cache, sections
    │   └── mongodb_atlas.py  # Atlas vs local Mongo
    ├── static/               # UI, favicons, login art
    │   └── img/login-dark.png
    ├── data/                 # seed / file fallback, icons/
    ├── docker-compose.yml
    ├── Dockerfile
    └── requirements.txt
```

---

## Configuration

Set these on the `apphub` service in `apphub-ui/docker-compose.yml`. There is no separate `.env` file.

| Variable | Default | Description |
|----------|---------|-------------|
| `APPHUB_USER` | `shlomo` | Login username |
| `APPHUB_PASSWORD` | `shlomo` | Login password |
| `TZ` | `Asia/Jerusalem` | Container timezone |
| `USE_MONGODB` | `true` | `true` = Mongo (Atlas or local). `false` = `data/apps.json` only |
| `MONGO_HOST` | `mongo` | Atlas hostname (`*.mongodb.net`) or local service name `mongo` |
| `MONGO_DB` | `apphub` | Database name |
| `MONGO_URI` | empty | Optional full URI (overrides host/user/password) |
| `MONGO_INITDB_ROOT_USERNAME` | — | Mongo user |
| `MONGO_INITDB_ROOT_PASSWORD` | — | Mongo password |

**Atlas (typical):** `USE_MONGODB=true` and `MONGO_HOST=<your-cluster>.mongodb.net`. Compose does not start a local `mongo` container.

**Local Mongo:** uncomment the `mongo` service and the `MONGO_HOST=mongo` block in `docker-compose.yml`.

**File only:** `USE_MONGODB=false`. Apps stay in `data/apps.json`; uploaded icons stay under `data/icons/`.

Do not commit real passwords. Put secrets in compose on the machine that runs the stack (or use env injection / secrets manager).

---

## Architecture

```
Browser  →  FastAPI (port 8092)  →  MongoDB Atlas database apphub
                                 →  or local Mongo
                                 →  or data/apps.json
```

| Piece | Role |
|-------|------|
| `apphub` (`apphub:v1.0.0`) | UI + REST API |
| Atlas / local Mongo | Apps, sections, icons, session secret |
| `data/apps.json` | Seed on first Atlas fill, or live store when Mongo is off |

Startup:

1. Connect to Mongo (Atlas `mongodb+srv`, or local `mongodb://`)
2. Ensure collections exist (`devops`, `networking`, `clouds`, `icons`, `settings`, `sections`, plus any custom section collections)
3. If app collections are empty, seed from `data/apps.json`
4. Download any pending custom icon URLs into `icons` as binary
5. Load apps, sections, and icon bytes into memory
6. Serve the UI on port 8092

---

## MongoDB Atlas (`apphub`)

| Collection | What you see |
|------------|----------------|
| `devops` | One document per DevOps-Tools app |
| `networking` | One document per Networking-Tools app |
| `clouds` | One document per Clouds app |
| `sections` | Tab list (built-in and custom) |
| *(your tab)* | One collection per custom section |
| `icons` | Custom icons only (URL metadata + image bytes) |
| `settings` | Login session secret (`_id: session`) |

Moving a tile to another tab deletes it from the old collection and writes it to the new one.

### App document

```json
{
  "_id": "grafana-k3s",
  "id": "grafana-k3s",
  "name": "Grafana",
  "url": "https://example.com/",
  "icon": "custom",
  "icon_file": "1cc23728a57f.png",
  "icon_url": ""
}
```

`icon` is `grafana` (legacy built-in) or `custom`. Custom uploads set `icon_file`. Custom URLs keep `icon_url` and also store bytes in `icons`.

### Icon document

```json
{
  "_id": "1cc23728a57f",
  "app_id": "1cc23728a57f",
  "name": "Argocd",
  "filename": "1cc23728a57f.png",
  "content_type": "image/png",
  "icon_file": "1cc23728a57f.png",
  "icon_url": "https://example.com/logo.png",
  "data": "<BSON Binary>"
}
```

Seed from file runs only when Atlas app collections are empty. Later edits to `data/apps.json` do not overwrite Atlas.

---

## Latency

Atlas may be far from your host, so a live read can take about 1–3 seconds. AppHub does **not** call Atlas on every click.

- On start it loads apps, sections, and icon images **once** into memory
- Tabs, search, listing apps, and showing icons use that snapshot
- Add / Edit / Remove / section changes still **write to Atlas**, then update memory
- The Mongo ping in the header is cached for 10 seconds
- The Mongo client keeps a warm pool (`minPoolSize=1`)

---

## HTTP API

Unauthenticated: `/`, `/static/*`, `POST /api/login`. All other `/api/*` routes need a session cookie.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | UI (`Cache-Control: no-store`) |
| `POST` | `/api/login` | `{ "username", "password" }` → user + Mongo status object |
| `POST` | `/api/logout` | Clear session |
| `GET` | `/api/me` | Current user + Mongo status, or `401` |
| `GET` | `/api/apps` | All tiles + sections + Mongo status |
| `GET` | `/api/sections` | Tab list |
| `POST` | `/api/sections` | `{ "name" }` create a tab |
| `PUT` | `/api/sections/{id}` | Rename a tab |
| `DELETE` | `/api/sections/{id}` | Remove a tab and its apps (last tab is kept) |
| `POST` | `/api/apps` | Create tile (`multipart/form-data`) |
| `PUT` | `/api/apps/{id}` | Update tile |
| `DELETE` | `/api/apps/{id}` | Remove tile and its icon |
| `GET` | `/api/icons/{filename}` | Custom icon bytes |

Create / update fields: `name`, `url`, `section`, `icon` (`grafana` / `custom`), `icon_url`, optional file `icon_file`.

Duplicate URL in the same section → `409`. Bad URL or empty custom icon → `400`.

Login / me return `mongodb` as an object, for example `{ "name": "mongodb-atlas", "connected": true }` (not a plain string).

---

## Recent changes (high level)

- Split login with hub art + Argo-style titles (**AppHub** top and bottom), underline fields, teal **SIGN IN**
- Narrower login panel; no vertical scroll on the login page
- Sections: create, rename, delete (keep at least one)
- Title-case section and app names on save
- Icon pipeline: Pillow resize to 256×256, rewrite on update, Atlas-backed serving
- Login screen no longer shows a marketing tagline
- App lives under `apphub-ui/` in this GitHub repo

---

## Notes

- Single shared login (no roles, no SAML/SSO).
- Custom icon URLs are downloaded server-side (timeout 15s, max 2MB). If download fails, the URL can still be saved and the tile may fall back to the remote image.
- Rebuild after static changes: `cd apphub-ui && docker compose up -d --build`, then hard-refresh.

---

## Troubleshooting

| Symptom | What to check |
|---------|----------------|
| Globe in the browser tab | Hard-refresh; favicon is `/static/img/favicon-32.png` |
| Custom icon white or cropped | Big photos are resized on upload; re-upload and hard-refresh |
| Login form flashes on refresh | `/api/me` should finish before the login screen is shown |
| Login page scrolls | Should be fixed; hard-refresh CSS (`styles.css?v=…`) |
| Slow tabs | First load after restart reads Atlas; later clicks should be memory |
| Unexpected logout | Idle timeout is 3 minutes; use **Stay signed in** |
| Atlas empty after restart | Seed runs only when built-in app collections have no documents |
| Local Mongo will not start | Uncomment the `mongo` service; `MONGO_HOST` must be `mongo` |
| Login `500` on Mongo status | Response must allow `mongodb` as an object (`dict[str, Any]`), not `dict[str, str]` |

---

FastAPI, Uvicorn, PyMongo, Pillow, Docker Compose, HTML/CSS/JavaScript.
