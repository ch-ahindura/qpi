# QPI dashboard

The React single-page app served at `/` by the QPI server. Built with Vite, React 19,
TypeScript and Tailwind; it talks to the server as a PocketBase client
(`src/lib/pb.ts`), so there is no bespoke API layer here.

`npm run build` writes `dist/`, which `qpi-ui/main.go` embeds with
`//go:embed all:internal/dashboard/dist`. The build is therefore a prerequisite of
the Go build, not an optional step — `make build-dashboard` runs it, and `make build`
depends on it. A stale or missing `dist/` is served as-is.

## Working on it

| Command | Does |
|---|---|
| `npm run dev` | Vite dev server with HMR. Points at `http://localhost:8090` for the API, so run `qpi serve` alongside it. |
| `npm run build` | Type-check (`tsc -b`) then bundle to `dist/`. |
| `npm run lint` | ESLint. |
| `npm run format` | Prettier over `src/`, `index.html` and the root configs. |
| `npm run cypress:run` | Cypress specs against `http://127.0.0.1:8090`, which must already be serving. |

Prefer `make test-e2e-dashboard` over `cypress:run` — it brings up a seeded server
first. `SPEC=<glob>` narrows it to one spec.

## Layout

- `src/App.tsx` — auth, the data loads, and the tab switch. Tabs are selected by URL
  hash (`#overview`, `#qpus`, `#drivers`, `#monitoring`, `#calibration`, `#jobs`,
  `#bookings`, `#settings`, `#admin`); an unrecognised hash is ignored rather than
  routed. `#drivers`, `#monitoring`, `#calibration` and `#admin` render only for a
  superuser.
- `src/components/tabs/<Name>Tab/` — one directory per tab, `index.tsx` plus its own
  `elements/`.
- `src/lib/ThemeContext.tsx` — applies the active theme's design tokens as CSS
  variables and injects its custom CSS/JS (RFC 0002; see [theming](../../../docs/theming.md)).
  Wraps `App` in `src/main.tsx`.
- `src/types.ts` — the shared record and payload types.
- `cypress/e2e/` — specs grouped by the tab they exercise.

Admin identity is `collectionName === "_superusers"` on the PocketBase auth store;
there is no separate role field.
