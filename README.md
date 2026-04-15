

## How to install?

```haskell
$ pip install -r requirements.txt
```


## Backend API (`backend.py`)

FastAPI server that scrapes Google Flights using the `fast-flights` library.

**Start the server:**
```bash
uvicorn backend:app --reload --port 8000
```

### Endpoints

| Method   | Path              | Description                                          |
|----------|-------------------|------------------------------------------------------|
| `POST`   | `/api/search`     | Search flights for a date range and routes           |
| `GET`    | `/api/progress`   | Polling endpoint to track search progress (0–100%)   |
| `GET`    | `/api/logs`       | Returns the in-memory log buffer                     |
| `POST`   | `/api/log-level`  | Change verbosity at runtime (`0`=silent … `3`=debug) |

### `POST /api/search` — request body

```json
{
  "fecha_ini": "02-06-2026",
  "fecha_fin": "13-06-2026",
  "airport_from": ["VLC"],
  "airport_to": ["AMS"],
  "max_stops": 1
}
```

- `fecha_ini` / `fecha_fin`: date range in `DD-MM-YYYY` format.
- `airport_from` / `airport_to`: lists of IATA codes (multiple airports supported).
- `max_stops`: `0` = direct only, `1` = up to 1 stop, etc.

### How it works

1. Builds every origin→destination pair, excluding same-airport routes.
2. For each route, queries every day in the range **in parallel** (thread pool, 3–8 workers).
3. Each day is retried up to 3 times. Returns the 3 cheapest flights per day, sorted by price.
4. Times are normalised to 24 h format.
5. Returns a flat list of `FlightResult` objects plus per-route summaries.

## Credits

Based on [fast-flights](https://github.com/AWeirdDev/flights) by [AWeirdDev](https://github.com/AWeirdDev), licensed under the [MIT License](LICENSE).