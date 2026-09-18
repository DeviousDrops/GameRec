# Steam catalogue APIs & free-tier LLM options

**Researched:** 2026-09-17. **Method:** Valve/vendor first-party docs, plus live endpoint probing from a VM (IP geolocated to India — noted where it matters). Every factual claim is cited. Claims I could not confirm from a first-party source are marked **[unverified]** with the best evidence I had.

> **Headline:** `ISteamApps/GetAppList/v2` is **gone** — it now returns HTTP 404, not just a deprecation warning. Any ingest design that assumes it works needs to change. See §1.1.

---

## Topic 1 — Steam APIs for a catalogue ingest

### 1.1 `ISteamApps/GetAppList/v2` — removed, not merely deprecated

Valve's Steamworks docs still list the method, but flag it: *"Deprecated - this API can no longer scale to the number of items available on Steam"*, directing readers to `IStoreService/GetAppList` ([ISteamApps docs](https://partner.steamgames.com/doc/webapi/ISteamApps)).

**Live behaviour is worse than "deprecated" — it 404s.** Probed 2026-09-17:

```
GET https://api.steampowered.com/ISteamApps/GetAppList/v2/
-> HTTP 404
   Method 'GetAppList' not found in interface 'ISteamApps'
```

This is not a transient outage or a network/geo artefact. Evidence:

1. All URL spellings fail identically: `/v2/`, `/v2`, `/v0002/`, `/v1/`, with and without `?format=json` — all HTTP 404, same body.
2. A **sibling method on the same interface works**: `ISteamApps/UpToDateCheck/v1/?appid=440&version=0` returns HTTP 200 with valid JSON. So the host and interface are reachable; only the method is missing.
3. `ISteamWebAPIUtil/GetSupportedAPIList/v1/` (keyless, HTTP 200) enumerates **only three** `ISteamApps` methods today: `GetSDRConfig`, `GetServersAtAddress`, `UpToDateCheck`. `GetAppList` is absent from the interface entirely. ([GetSupportedAPIList](https://api.steampowered.com/ISteamWebAPIUtil/GetSupportedAPIList/v1/))
4. **404 vs 403 is diagnostic.** `IStoreService/GetAppList/v1/` without a key returns HTTP **403** — *"Access is denied. Retrying will not help. Please verify your `key=` parameter."* — i.e. the method exists but needs auth. `ISteamApps/GetAppList` returns **404 "not found in interface"**, i.e. the method does not exist. Different failure modes, so the 404 is not an auth problem.

Corroborating third-party timeline: the deprecation notice was observed from ~2025-11-25 ([woctezuma/steam-store-snapshots](https://github.com/woctezuma/steam-store-snapshots)), and other developers hit the same 404 in 2026 ([SteamAutoCracker issue #117](https://github.com/BigBoiCJ/SteamAutoCracker/issues/117)).

**Answers to the original questions, for the record:**

| Question | Answer |
|---|---|
| Response shape | Was `{"applist":{"apps":[{"appid":int,"name":str}, ...]}}`. **[unverified]** against live traffic — endpoint is dead. Shape is corroborated by the analogous `GetPartnerAppListForWebAPIKey` schema in the [ISteamApps docs](https://partner.steamgames.com/doc/webapi/ISteamApps) (`applist` → `apps` → `app`). |
| How many apps today | **N/A — returns nothing.** See catalogue sizing below. |
| Pagination / deltas | None. It was a single unpaginated dump; that is exactly why Valve says it "can no longer scale". |
| Key required | No — it was anonymous. That is also now moot. |

#### Replacement: `IStoreService/GetAppList/v1`

Documented at [partner.steamgames.com/doc/webapi/IStoreService](https://partner.steamgames.com/doc/webapi/IStoreService). **Requires a key** ("Any web API key" — an ordinary Web API key works, not only a publisher key).

| Param | Type | Notes (verbatim from docs) |
|---|---|---|
| `key` | string | **Required.** "Any web API key." |
| `if_modified_since` | uint32 | "Return only items that have been modified since this date." |
| `have_description_language` | string | "Return only items that have a description in this language." |
| `include_games` | bool | "Include games (defaults to enabled)" |
| `include_dlc` | bool | "Include DLC" |
| `include_software` | bool | "Include software items" |
| `include_videos` | bool | "Include videos and series" |
| `include_hardware` | bool | "Include hardware" |
| `last_appid` | uint32 | "For continuations, this is the last appid returned from the previous call." |
| `max_results` | uint32 | "Number of results to return at a time. Default 10k, max 50k." |

This gives you what v2 never had: **real pagination** (`last_appid` cursor, results ordered by appid) and **real deltas** (`if_modified_since`, plus per-item `last_modified` and `price_change_number` fields). For an incremental catalogue ingest this is strictly better than the old dump.

**[unverified]:** I could not exercise this endpoint — no API key available in this environment. Confirmed only that it exists and rejects keyless calls with 403. Note also the docs show the `partner.steam-api.com` host; the `api.steampowered.com` host also resolves the method (403 rather than 404), and community usage points at `api.steampowered.com` ([steam-store-snapshots](https://github.com/woctezuma/steam-store-snapshots)).

**Note:** by default `IStoreService` returns **games only** — you must opt into DLC/software/video/hardware. Historically this made it much smaller than the old `ISteamApps` list: ~48,792 vs ~107,911 apps as of 2021-01-09 ([steam-store-snapshots](https://github.com/woctezuma/steam-store-snapshots)).

#### How big is the catalogue today?

Since `GetAppList` is dead, I sized it from Valve's own store search endpoint (`store.steampowered.com/search/results/?infinite=1`, which returns `total_count`). Measured 2026-09-17, `cc=us&l=english`:

| Scope | `total_count` |
|---|---|
| All store products | **285,388** |
| Games only (`category1=998`) | **176,952** |
| DLC only (`category1=21`) | **57,868** |

These are **store-visible** products, so they undercount the full appid space (tools, servers, delisted and unreleased apps). Treat as a lower bound on what you'd want to ingest, and an upper bound on what `appdetails` will usefully return.

---

### 1.2 `store.steampowered.com/api/appdetails` (unofficial)

Not documented by Valve anywhere — this is the store frontend's own JSON endpoint. Everything below is **empirically verified** on 2026-09-17 unless noted.

#### Parameters

| Param | Verified behaviour |
|---|---|
| `appids` | Comma-separated. **Multiple appids only work with `filters=price_overview`** — see below. |
| `cc` | Country code; controls currency and regional price. Verified: `cc=us` → `"currency":"USD"`, `cc=de` → `"EUR"`, `cc=jp` → `"JPY"`. **Omitting `cc` geolocates by caller IP** — from this VM it defaulted to `INR`. Always set `cc` explicitly for reproducible ingests. |
| `l` | Language; controls localised `name`/`short_description`/`detailed_description`. Verified `l=english` vs `l=german` returns German prose. |
| `filters` | Comma-separated field selector, e.g. `filters=basic`. Restricts returned keys. |

#### Multiple appids per call — the real rule

I tested 18 filter values against a 2-appid request. **Only `price_overview` works.** Everything else returns **HTTP 400 with the body `null`**:

| `filters=` value (2 appids) | Result |
|---|---|
| `price_overview` | **HTTP 200**, per-appid keyed object, data populated |
| `basic`, `genres`, `categories`, `release_date`, `developers`, `publishers`, `platforms`, `metacritic`, `recommendations`, `achievements`, `screenshots`, `movies`, `dlc`, `packages`, `controller_support`, `content_descriptors`, `supported_languages` | HTTP 400, body `null` |
| *(no `filters` param)* | HTTP 400, body `null` |

Working multi-appid response shape:

```json
{"1091500":{"success":true,"data":{"price_overview":{"currency":"USD","initial":5999,"final":5999,
  "discount_percent":0,"initial_formatted":"","final_formatted":"$59.99"}}},
 "292030":{"success":true,"data":{"price_overview":{}}}}
```

Caveat: for free games, multi-appid `price_overview` returns `"data":[]` — an **empty JSON array, not an object** (verified on appids 440/570/730, all free). Your parser must tolerate `data` being `{}`, `[]`, or absent.

**Design implication:** batching only helps for price refreshes. Full metadata ingest is strictly **one appid per request**, which is what makes the rate limit (§1.4) the binding constraint.

#### Response shape (single appid)

```json
{"440": {"success": true, "data": {}}}
```

Unknown/unavailable appid returns `{"999999999":{"success":false}}` — HTTP **200**, `success:false`, and **no `data` key at all**.

Full key set observed on appid 440: `about_the_game, achievements, background, background_raw, capsule_image, capsule_imagev5, categories, content_descriptors, detailed_description, developers, dlc, genres, header_image, is_free, linux_requirements, mac_requirements, metacritic, movies, name, package_groups, packages, pc_requirements, platforms, publishers, ratings, recommendations, release_date, required_age, screenshots, short_description, steam_appid, support_info, supported_languages, type, website`.

#### `type` values

Verified live: `game` (440, 1085660, 2379780, 1966720), `dlc` (2138330 — Cyberpunk 2077: Phantom Liberty), `music` (2678630 — CS2 Soundtrack; 323130 — Half-Life Soundtrack).

**[unverified]:** `demo`, `video`, `series`, `hardware`, `mod`, `episode`, `advertising`. Valve publishes no enumeration of `appdetails.type` values. Indirect first-party support: `IStoreService/GetAppList` has `include_games` / `include_dlc` / `include_software` / `include_videos` / `include_hardware` flags ([IStoreService docs](https://partner.steamgames.com/doc/webapi/IStoreService)), implying at least those categories exist as app kinds. Best approach: treat `type` as an open string, don't enum-validate it.

#### Field map for the fields you asked about

| Need | Field | Shape (verified on appid 1091500) |
|---|---|---|
| Name | `name` | `"Cyberpunk 2077"` |
| Short description | `short_description` | Plain text, localised by `l`. |
| Long description | `detailed_description` | **HTML** (`<h1>`, `<p class="bb_paragraph">`, `<img>`). Also `about_the_game` — a separate, usually similar HTML blob. Must be stripped/sanitised. |
| Genres | `genres` | `[{"id":"3","description":"RPG"}]` — **`id` is a string**. |
| Categories | `categories` | `[{"id":2,"description":"Single-player"}, ...]` — **`id` is an int**. Inconsistent with `genres`. |
| Release date | `release_date` | `{"coming_soon":false,"date":"Dec 9, 2020"}` — date is a **localised display string**, not ISO. Format varies with `l`. Parse defensively. |
| Price | `price_overview` | `{"currency","initial","final","discount_percent","initial_formatted","final_formatted"}`. Integer minor units (`5999` = $59.99). **Absent entirely when `is_free:true`** — check `is_free` first. |
| Adult content | `content_descriptors` | `{"ids":[1,2,5],"notes":"Cyberpunk 2077 contains strong language, intense violence, blood and gore, as well as nudity and sexual material.\r\n"}` |
| Adult content (2) | `required_age` | See type warning below. |
| Ratings | `ratings` | Per-board dict: `esrb, pegi, oflc, nzoflc, kgrb, dejus, steam_germany, igrs`, varying by app. |

**Gotcha — `required_age` has an inconsistent JSON type.** Verified across five apps in one batch:

| appid | value | JSON type |
|---|---|---|
| 440 | `0` | int |
| 1085660 | `0` | int |
| 2379780 | `0` | int |
| 1091500 | `"17"` | **string** |
| 292030 | `"17"` | **string** |

Coerce with `int(str(v) or 0)`.

**[unverified] — `content_descriptors.ids` numeric meanings.** Valve's [Content Survey](https://partner.steamgames.com/doc/gettingstarted/contentsurvey) documents that a Mature Content disclosure exists and that developers must disclose adult content "even if it's not accessible or presented in your product", but **does not publish the numeric ID mapping**. The widely-repeated community mapping (1 = Some Nudity or Sexual Content, 2 = Frequent Violence or Gore, 3 = Adult Only Sexual Content, 4 = Frequent Nudity or Sexual Content, 5 = General Mature Content) is folklore. It is *consistent* with the `notes` string on 1091500 (`ids:[1,2,5]`, notes mention nudity/sexual material, violence/gore, strong language), which is weak supporting evidence, but I could not confirm it first-party. **Recommendation: key adult-detection off `notes` text and `required_age`/`ratings`, not off bare `ids`.**

**Gotcha — some apps silently return `success:false`.** Verified: appid 1229230 returns `success:false` with no data. Setting a `birthtime` / `mature_content` cookie did **not** help. **[unverified]** as to cause — could be age-gating, regional restriction (this VM is India-geolocated), or delisting; I could not isolate which. Budget for a non-trivial `success:false` rate and log appids that fail so you can distinguish permanent from transient.

---

### 1.3 `store.steampowered.com/appreviews/<appid>`

Also unofficial/undocumented by Valve. All verified live 2026-09-17 against appid 1091500.

#### Parameters

| Param | Verified behaviour |
|---|---|
| `json` | **`json=1` is required.** Omitting it returns `{"success":1,"html":"<div>..."}` — a rendered HTML blob, not structured review data. |
| `filter` | `recent`, `updated`, `all` all return reviews and a real cursor. **`summary` is special**: returns 10 reviews and `cursor:"*"` — it does not paginate. |
| `language` | `english`, `french`, `all`, etc. **Default is effectively `english`, NOT `all`** (verified: omitting `language` gave identical totals to `language=english`). Pass `language=all` explicitly for global figures. |
| `num_per_page` | **Hard cap 100.** Verified: asked 20 → got 20; asked 100 → got 100; asked 200 → got **100**. |
| `cursor` | See below. Must be URL-encoded (values contain `+`, `/`, `=`). |
| `review_type` | `all`, `positive`, `negative`. Verified to filter correctly (`positive` → `voted_up:true`, `negative` → `voted_up:false`). |
| `purchase_type` | `all`, `steam`, `non_steam_purchase`. **Default is `steam`** (verified: no param = `purchase_type=steam` = 390,549 total; `all` = 418,070). |
| `day_range` | Works with `filter=all`. |

#### Cursor pagination

Start with `cursor=*`. Each response carries a `cursor` for the next page. Verified over three pages at `num_per_page=2` — no overlap between pages:

```
page0  cursor_in='*'                      ids=[235464569, 235464511]  next='AoJw/9/m16ADf/Ocggc='
page1  cursor_in='AoJw/9/m16ADf/Ocggc='   ids=[235464313, 235460913]  next='AoJwudPW16ADcZObggc='
page2  cursor_in='AoJwudPW16ADcZObggc='   ids=[235460770, 235457421]  next='AoJwkO3E16ADfbiZggc='
```

**Termination:** when exhausted, the endpoint returns `reviews: []` and echoes the cursor back unchanged. Verified on a zero-review app (2662210): `n=0`, `cursor='*'`, `success:1`. **Stop when the returned cursor equals the one you sent, or when `reviews` is empty** — do not rely on a count field.

#### `query_summary`

Full form, **first page only**:

```json
{"num_reviews": 3, "review_score": 8, "review_score_desc": "Very Positive",
 "total_positive": 371926, "total_negative": 46144, "total_reviews": 418070}
```

**Critical:** on pages 2+, `query_summary` collapses to just `{"num_reviews": N}`. The totals appear **only on the `cursor=*` request**. Capture them on the first call.

Same collapse happens with `review_type=positive|negative` — verified, totals are suppressed even on the first page. **To get summary stats you must use `review_type=all` (or omit it) with `cursor=*`.**

**Totals are scoped by your filters,** which is easy to get wrong. Same app, same moment:

| `language` | `purchase_type` | `total_reviews` |
|---|---|---|
| `all` | `all` | **980,397** |
| `all` | `steam` | 882,944 |
| `all` | `non_steam_purchase` | 97,435 |
| `english` | `all` | 418,070 |
| `english` | `steam` (default) | 390,549 |
| `french` | `all` | 18,293 |

`review_score` (8) and `review_score_desc` ("Very Positive") stayed constant across all of these — they appear to reflect the global score, not the filtered slice. For a comparable cross-title review signal, pin `language=all&purchase_type=all`.

`num_per_page=1` is enough to harvest `query_summary` cheaply — you do not need to pull review bodies to get the score.

#### Per-review fields

`recommendationid, author, review, language, timestamp_created, timestamp_updated, voted_up, votes_up, votes_funny, weighted_vote_score, comment_count, steam_purchase, received_for_free, written_during_early_access, refunded, hardware, primarily_steam_deck, reactions, app_release_date`.

`author` sub-object: `steamid, num_games_owned, num_reviews, playtime_forever, playtime_last_two_weeks, playtime_at_review, last_played, personaname, avatar, profile_url, persona_status`.

---

### 1.4 Rate limits — documented vs. measured

#### What Valve actually documents

- **Steam Web API (`api.steampowered.com`): 100,000 calls/day.** From the [Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms): *"You are limited to one hundred thousand (100,000) calls to the Steam Web API per day."* Plus: *"You may not use the Steam Web API in any way that degrades the operation or performance of Steam or any games distributed via Steam."*
- [steamcommunity.com/dev](https://steamcommunity.com/dev) documents **no** rate limits at all — only that *"All use of the Steam Web API requires the use of an API Key."*
- **For `store.steampowered.com` endpoints (`appdetails`, `appreviews`): Valve documents nothing whatsoever.** These are not part of the Web API and are not covered by the 100k/day figure or any published policy.

#### What I measured (2026-09-17, single IP)

**Test 1 — burst to first throttle.** Sequential `appdetails` calls, one appid each, no artificial delay (~2.6 req/s achieved):

- Requests 1–247: **HTTP 200**
- Request **248**: **HTTP 429 Too Many Requests**, after 95.9 s

Verbatim response headers on the 429:

```
HTTP/1.1 429 Too Many Requests
Server: nginx
Content-Type: application/json; charset=utf-8
Content-Encoding: gzip
Vary: Accept-Encoding
Strict-Transport-Security: max-age=63072000
Content-Length: 24
Date: Thu, 17 Sep 2026 13:52:41 GMT
Connection: keep-alive
Set-Cookie: steamCountry=IN%7C...
```

So, answering the specific questions:

| Question | Measured answer |
|---|---|
| Status when throttled | **429**, not 403. |
| `Retry-After` sent? | **No.** Full header set above contains no `Retry-After`, and no `X-RateLimit-*` headers either. You must back off blind. |
| Body when throttled | `null` (gzipped, 24 bytes on the wire). Note it is **valid JSON** that parses to `None` — a naive `json.loads()` will succeed and yield nothing. **Check the status code, not the parse result.** |

**Test 2 — how long is the "soft ban"?** Polled every 15 s after the first 429:

```
t+  0.3s -> 429
t+ 15.6s -> 200   (recovered)
```

**~16 seconds, not 5 minutes.** This contradicts the common folklore that a throttle means a fixed multi-minute IP ban.

**Test 3 — sustained throughput.** Hammered `appdetails` continuously for 300 s, tallying per 30 s bucket:

| Window | 200s | 429s | Success rate |
|---|---|---|---|
| 0–30 s | 1 | 79 | 1% |
| 30–60 s | 0 | 78 | 0% |
| 60–90 s | 1 | 79 | 1% |
| 90–120 s | 1 | 86 | 1% |
| 120–150 s | 1 | 87 | 1% |
| 150–180 s | 12 | 77 | 13% |
| 180–210 s | **88** | 0 | 100% |
| 210–240 s | **87** | 0 | 100% |
| 240–270 s | 16 | 69 | 19% |
| 270–300 s | 1 | 83 | 1% |
| **Total** | **208** | **639** | 25% |

**Sustained successful throughput: 208 requests per 5 minutes (~42/min, ~0.69/s).**

#### Verdict on the "~200 requests per 5 minutes" figure

**The number is right; the mechanism commonly described is wrong.** My measured sustained ceiling was **208 per 5 minutes**, within 4% of the cited 200/5min. That is the strongest evidence I can offer *for* the figure, and it is first-hand rather than folklore.

But the widely-repeated model — "exceed it and you're IP-banned for 5 minutes" — does not match observed behaviour:

- You can **burst ~247 requests** well above the sustained rate before the first 429.
- Recovery after a 429 was **~16 s**, not 5 minutes.
- Under sustained load the endpoint **alternates** between fully-open windows (88 consecutive successes in 30 s) and fully-closed ones, rather than applying a flat ban.

This is consistent with a **token-bucket / sliding-window limiter refilling at roughly 40/min with a burst capacity around 240–250**, not a punitive ban. **[unverified]** — that's my inference from the response pattern; Valve publishes no limiter description, and I did not attempt to reverse-engineer exact bucket parameters.

**Practical guidance:** pace at **~35 req/min (one request per ~1.7 s)** and you should essentially never see a 429. On 429, sleep ~20–30 s and retry — do not sleep 5 minutes, you'd be leaving ~90% of your budget unused. At 35/min, a full 176,952-game ingest is ~84 hours single-threaded. Budget for a multi-day initial backfill, or seed from a mirror (§1.5) and use `appdetails` only for enrichment/refresh.

**Caveats on these measurements:** single IP, single geographic origin (India), single session, one-off test. Limits may differ by region, by endpoint, or over time.

**[unverified] — do `appdetails` and `appreviews` share a bucket?** I tried to test this but the budget had already refilled by the time I probed; `appreviews` then absorbed 60 rapid requests with zero 429s while `appdetails` was simultaneously healthy. That is consistent with either separate buckets or a shared-but-refilled one. **Not determined.** Assume shared and pace globally until proven otherwise.

---

### 1.5 Bulk mirrors and dumps

#### SteamSpy — [steamspy.com/api.php](https://steamspy.com/api.php)

Verified working 2026-09-17.

| Endpoint | Params | Notes |
|---|---|---|
| `request=all` | `page` (from 0) | **1,000 entries/page**, ordered by owners. Verified: page 0 returned exactly 1000 entries, 355 KB. |
| `request=appdetails` | `appid` | Verified HTTP 200 for appid 440. |
| `request=genre` | `genre` | e.g. `Early+Access` |
| `request=tag` | `tag` | |
| `request=top100in2weeks` / `top100forever` / `top100owned` | — | |

**Documented rate limits, verbatim:** *"Allowed poll rate - 1 request per second for most requests, 1 request per 60 seconds for the *all* requests."* Also: *"The data is refreshed once a day, there is no reason to request the same information more than once every 24 hours."*

Fields returned: `appid, name, developer, publisher, score_rank, positive, negative, userscore, owners, average_forever, average_2weeks, median_forever, median_2weeks, ccu, price, initialprice, discount, tags, languages, genre`.

**Fit for purpose — important limitations:**
- **Carries `positive`/`negative` review counts and `tags`** — genuinely useful, and obtainable in 1,000-app pages instead of one-at-a-time.
- **Does NOT carry** `short_description`, `detailed_description`, `release_date`, `categories`, or content/adult descriptors. It will **not** replace `appdetails` for narration-quality metadata.
- At 1 req/60 s for `all`, paging the full catalogue takes ~285 pages ≈ **~4.75 hours** — far better than ~84 hours of `appdetails`. Good seeding strategy.
- `appid: 999999` signals data withheld at the developer's request.

**Licence: none.** The page carries **no licence grant, no terms of service, no attribution requirement, and no copyright notice**. The only contact is the author's email (Sergey Galyonkin). **This is a licensing gap, not a permissive licence** — for a public project, treat redistribution of SteamSpy-derived data as unresolved and ask before republishing. Reliability: one-person hobby project, single point of failure, data refreshed daily; owner figures are estimated ranges, not exact.

#### woctezuma/steam-store-snapshots — [github.com/woctezuma/steam-store-snapshots](https://github.com/woctezuma/steam-store-snapshots)

Point-in-time JSON snapshots of Steam app lists from both `ISteamApps` and `IStoreService`. **Licence: MIT** — the cleanest licensing of the options here. Caveat: these are **appid + name only**, not full metadata, and the repo's own documented figures are from 2021-01-09 (107,911 / 48,792), so freshness must be checked before relying on it. Useful as a keyless substitute for the dead `GetAppList`.

#### GameDataCrunch

Referenced by the above repo as an unofficial API for the Steam store app list without needing a key, with a `gamedatacrunch` PyPI package. **[unverified]** — I did not test it, and did not verify its licence, provenance or reliability. Best evidence is a third-party README mention only.

#### SteamDB

Commonly suggested, but **[unverified] and not recommended**: SteamDB has no public API, and scraping it is against its terms. Do not design around it.

---

## Topic 2 — Free-tier LLM APIs for short narration

Use case: ~50–200 tokens in, ~150 tokens out, a few requests/min, latency-sensitive (sub-2s), from a VM.

### Summary table

| Provider | Genuinely free, no card? | Documented free limits | OpenAI-compatible | Trains on free input? |
|---|---|---|---|---|
| **Google Gemini** | **Yes** | **Numbers no longer published** — see below | Yes | **Yes** |
| **Groq** | Likely yes **[unverified]** | 30 RPM / 1,000 RPD / 8K TPM (gpt-oss) | Yes | **No** (contractual) |
| **Cerebras** | **No — card required** | 5 RPM / 30K–90K TPM / 1M TPD | Yes | No (policy) |
| **OpenRouter** | **Yes** | 20 RPM / 50 RPD (<10 credits) | Yes | Configurable |
| **Mistral** | Yes | **Numbers no longer published** | Partial | **Yes, by default** |

---

### Google Gemini

**Free tier: yes, no card.** New accounts start on the Free Tier, qualifying via an *"Active project or free trial"* with a billing tier cap of "N/A"; leaving the free tier requires linking billing and ≥$5 credits ([billing docs](https://ai.google.dev/gemini-api/docs/billing)). Free tier is available in the EEA, UK and Switzerland ([billing docs](https://ai.google.dev/gemini-api/docs/billing)).

**Rate limits: Google has removed the per-model free-tier table from the public docs.** [ai.google.dev/gemini-api/docs/rate-limits](https://ai.google.dev/gemini-api/docs/rate-limits) now states only: *"Rate limits depend on a variety of factors (such as your usage tier) and can be viewed in Google AI Studio"*, with *"View your active rate limits in AI Studio"* and the warning *"Specified rate limits are not guaranteed and actual capacity may vary."* I fetched the raw page and extracted every table: the only tables remaining are the spend-rate-limit and qualification tables (Free / Tier 1 / Tier 2 / Tier 3). **There is no free-tier RPM/TPM/RPD table on the page any more.**

**[unverified] — specific Gemini free-tier RPM/RPD numbers.** The authoritative source is now `aistudio.google.com/rate-limit`, which is behind a login I don't have. Best available evidence: the docs confirm limits are measured as **RPM / TPM (input) / RPD**, are applied **"per project, not per API key"**, and *"Requests per day (RPD) quotas reset at midnight Pacific time."* Any specific figure you've seen quoted (e.g. "15 RPM / 1,500 RPD for Flash") should be re-checked in AI Studio against your own project before you design around it.

**Free models:** The [pricing page](https://ai.google.dev/gemini-api/docs/pricing) marks input/output as "Free of charge" for a wide set, including **Gemini 3.8 / 3.7 / 3.6 / 3.5 Flash**, **Gemini 3.5 and 3.1 Flash-Lite**, **Gemini 3 Flash Preview**, **Gemini 2.5 Flash / Flash-Lite / Pro**, and **Gemma 4** (free only — no paid tier). The Flash-Lite models are free across Standard, Batch, Flex and Priority.

**Training on free input: YES — this is the key risk for a public project.** Every model's pricing table has a row *"Used to improve our products"* reading **"Yes" on free and "No" on paid** ([pricing](https://ai.google.dev/gemini-api/docs/pricing)). The plan cards restate it: free = *"Content used to improve our products"*, paid = *"Content **not** used to improve our products"*. The billing doc confirms that moving to paid is how you *"ensure your prompts and responses are not used to improve Google products."*

**Access:** Python SDK (`google-genai`) plus an **OpenAI-compatible endpoint** at `https://generativelanguage.googleapis.com/v1beta/openai/` ([OpenAI compat docs](https://ai.google.dev/gemini-api/docs/openai)). Docs note the compatibility layer is "in beta".

---

### Groq

**Free tier: yes, with a published limits table.** From [console.groq.com/docs/rate-limits](https://console.groq.com/docs/rate-limits), "Free Plan Limits" tab:

| Model ID | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| `openai/gpt-oss-120b` | 30 | 1K | 8K | 200K |
| `openai/gpt-oss-20b` | 30 | 1K | 8K | 200K |
| `openai/gpt-oss-safeguard-20b` | 30 | 1K | 8K | 200K |
| `qwen/qwen3.8-27b` | 30 | 1K | 8K | 200K |
| `groq/compound` | 30 | 250 | 70K | – |
| `groq/compound-mini` | 30 | 250 | 70K | – |
| `whisper-large-v3` | 20 | 2K | – | – |
| `whisper-large-v3-turbo` | 20 | 2K | – | – |
| `meta-llama/llama-prompt-guard-2-22m` / `-86m` | 30 | 14.4K | 15K | 500K |

Limits are enforced **per organization**; cached tokens are excluded; exceeding yields **429 with a `retry-after` header** ([rate limits](https://console.groq.com/docs/rate-limits)).

For your workload (~350 tokens/request round trip), 1,000 RPD and 8K TPM are comfortable: 8K TPM ÷ ~200 input tokens ≈ 40 requests/min of headroom, well above the 30 RPM cap, and 30 RPM far exceeds "a handful per minute".

**Training: No.** The [Groq Services Agreement](https://console.groq.com/docs/legal/services-agreement) states Groq *"is not permitted to use Inputs or Outputs for training or fine-tuning any AI Model Services or other models"* unless explicitly permitted. This is **not** written as tier-dependent. [Your Data in GroqCloud](https://console.groq.com/docs/your-data) adds: *"By default, Groq does not retain customer data for inference requests"*, with exceptions only for retention-dependent features (batch, fine-tuning) and reliability/abuse troubleshooting, where logs are kept *"for up to 30 days"*. *"All customers may enable Zero Data Retention (ZDR)"* — no tier carve-out stated. **This is the strongest privacy posture of the five for a public project.**

**[unverified] — whether the Groq free tier requires a card.** The rate-limits page documents free limits but says nothing about payment method; the [quickstart](https://console.groq.com/docs/quickstart) describes creating a key with no mention of billing; [groq.com](https://groq.com/pricing) has no tier table. Best evidence: a documented "Free Plan" with concrete limits and a signup flow that never mentions payment — but I found **no explicit first-party "no credit card required" statement.**

**Access:** Python SDK (`pip install groq`) and **OpenAI-compatible** at `https://api.groq.com/openai/v1` ([OpenAI compat](https://console.groq.com/docs/openai)). Unsupported: `logprobs`, `logit_bias`, `top_logprobs`, `messages[].name`; `N` must be 1; `temperature=0` is silently rewritten to `1e-8`.

---

### Cerebras

**No genuinely free tier — a card is required.** Per [inference-docs.cerebras.ai/support/rate-limits](https://inference-docs.cerebras.ai/support/rate-limits), new accounts get **$5 in free credits after adding a verified payment method**, and those credits expire 30 days after issue. If you decline to add payment details at signup, *"Playground and API access remain inactive until you do."* The docs explicitly state there is **no always-free, auto-renewing tier** — once trial credits are spent or expired, access pauses until you purchase credits.

**Free Trial limits:**

| Model | RPM | Uncached TPM | Total TPM | TPH | TPD |
|---|---|---|---|---|---|
| `gpt-oss-120b` | 5 | 30K | 90K | 1M | 1M |
| `qwen-3.8-27b` | 5 | 30K | 90K | 1M | 1M |

**5 RPM is tight** for "a handful of requests per minute" — it's right at the boundary.

**Training:** The [Cerebras Privacy Policy](https://www.cerebras.ai/privacy-policy) states *"We do not retain inputs and outputs associated with our training, inference and chatbot Services."* The [Terms of Use](https://www.cerebras.ai/terms-of-service) reserve rights over *Usage Data* (for improving products, shared only de-identified/aggregated) but do not on their face grant rights to train on prompt content. **[unverified]** — no explicit "we do not train on your inputs" sentence found; the retention statement is the closest first-party evidence, and the Cloud page's *"never stored, logged, or reused"* is marketing copy rather than terms.

**Access:** OpenAI-compatible at `https://api.cerebras.ai/v1` ([docs](https://inference-docs.cerebras.ai/resources/openai)); `n` must be 1.

---

### OpenRouter free models

**Free tier: yes, no card** (though buying credits raises the daily cap). From [openrouter.ai/docs/api-reference/limits](https://openrouter.ai/docs/api-reference/limits), for model ids ending in `:free`:

| Lifetime credits purchased | Requests/min | Requests/day |
|---|---|---|
| Under 10 | **20** | **50** |
| 10 or more | 20 | **1,000** |

The tier is chosen by **all-time credits purchased**, not current balance, and the higher ceiling kicks in at 9 credits to absorb rounding. `GET /api/v1/key` exposes `free_model_daily_requests` with `used`/`limit`/`remaining`. 429s carry `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`. Note a **negative credit balance yields 402 errors even on free models**.

**50 requests/day without paying is the binding constraint** — that is a hard blocker for catalogue-scale narration, though fine for prototyping. A one-time ~$10 purchase lifts it to 1,000/day permanently.

**Free models available today** (live from [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models), 2026-09-17 — 20 of 444 models have `:free` ids, all with `prompt`/`completion` pricing of 0):

`cohere/north-mini-code:free`, `dots-studio/dots-3-note-preview:free`, `google/gemma-4-26b-a4b-it:free`, `google/gemma-4-31b-it:free`, `inclusionai/ling-3.0-flash-fin:free`, `inclusionai/ling-3.0-flash-sante:free`, `inclusionai/ling-3.0-flash-vl:free`, `liquid/lfm-2.5-2.6b:free`, `nex-agi/nex-n2.5-mini:free`, `nex-agi/nex-n2.5-pro:free`, `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`, `nvidia/nemotron-3-super-120b-a12b:free`, `nvidia/nemotron-3-ultra-550b-a55b:free`, `nvidia/nemotron-3.5-content-safety:free`, `nvidia/nemotron-3.5-lightning:free`, `poolside/laguna-s-2.1:free`, `poolside/laguna-xs-2.1:free`, `thinkingmachines/inkling-small:free`, `thinkingmachines/inkling:free`, `z-ai/glm-5.2:free`

For short narration, `liquid/lfm-2.5-2.6b:free` (2.6B, 64K ctx) and `google/gemma-4-26b-a4b-it:free` (MoE, 4B active) are the latency-sensible picks. **This list churns frequently — re-query the models endpoint rather than hardcoding.**

**Training:** configurable. [Privacy and logging docs](https://openrouter.ai/docs/features/privacy-and-logging): *"There are separate settings for paid and free models"*, and *"If you opt out of training in your account settings, OpenRouter will not route to providers that train."* Also: *"Wherever possible, OpenRouter works with providers to ensure that prompts will not be trained on, but there are exceptions."* **[unverified]** — I found no first-party statement that free endpoints *must* publish or train on prompts, contrary to a common belief; the docs describe an opt-out toggle rather than a mandatory opt-in.

**Access:** OpenAI SDK works as a **"drop-in replacement"** at `https://openrouter.ai/api/v1` ([quickstart](https://openrouter.ai/docs/quickstart)).

---

### Mistral

**Free tier: yes ("Free mode", the default).** [Usage and limits](https://docs.mistral.ai/admin/user-management-finops/tier): *"Free mode lets you create API keys and use included monthly usage within the limits shown on the Limits page."*

**[unverified] — numeric free-tier limits.** Like Google, **Mistral no longer publishes them.** The docs name only the limit *categories* — *"Tokens per minute: the token throughput limit for the model"* and *"Requests per second: the request throughput limit for the model"*, plus tokens-per-month — and direct you to Admin Panel → API → Limits for actual values ([tier docs](https://docs.mistral.ai/admin/user-management-finops/tier), [help article](https://help.mistral.ai/en/articles/698531-why-am-i-hitting-api-rate-limits-and-how-do-i-increase-them)). Best available evidence: *"Free mode (the default) has the lowest limits, intended for evaluation and prototyping"*, limits are applied **per model** and **per organization**, and exceeding them returns 429. Historical community figures (e.g. 1 RPS / 500K tokens per minute) are **not** currently confirmable first-party.

Note a non-obvious rule: *"Prepaying credits adds funds to your account; it does not change your tier or rate limits"* — tiers rise on **cumulative billed amount**, not deposits.

**[unverified] — card/phone requirement.** Neither the tier docs nor the rate-limit help article mention phone verification or a card for Free mode; they treat it simply as "the default". I found no first-party confirmation either way.

**Models:** current small/fast open-weight models are **Mistral Small 4** (Apache 2.0, v26.03) and **Ministral 3** in 14B / 8B / 3B (Apache 2.0, v25.12) ([models overview](https://docs.mistral.ai/getting-started/models/models_overview)). **[unverified]** — that page does **not** publish the current API id strings (it only lists ids for deprecated models like `ministral-8b-2410`, `mistral-small-2506`). Resolve current ids from the API reference or `/v1/models` against your own key rather than guessing.

**Training on free input: YES, by default.** [Mistral Help Center](https://help.mistral.ai/en/articles/347617-do-you-use-my-user-data-to-train-your-artificial-intelligence-models): for Mistral Studio (API) **Free mode**, *"we may use your data (input and output)"* to train models, with *"the right to opt out of this program at any time"*. Pay-as-you-go emphasises user control; Enterprise is opted out by default. Opt-out for API/Studio is via the **Privacy menu in the Admin panel** ([privacy and data controls](https://docs.mistral.ai/admin/monitor-comply/privacy-data-controls)). Two caveats: thumbs-up/down **feedback is used regardless of tier**, and activating **Labs models overrides opt-out** on any plan.

**Access:** Python SDK (`mistralai`) and plain REST. OpenAI compatibility is **partial** — Mistral's native API is the documented path. **[unverified]** — I did not confirm a first-party OpenAI-compatible base URL for Mistral, unlike the other four.

---

### Ranking for this use case

Criteria: ~50–200 tokens in / ~150 out, a handful of req/min, sub-2s, from a VM, public project.

**1. Groq — clear first choice.** The only option that is simultaneously (a) free with concrete published limits, (b) contractually **non-training** with default zero retention, and (c) architecturally the fastest of the group. 30 RPM and 1,000 RPD comfortably cover the workload. `openai/gpt-oss-20b` is the right model — smallest capable chat model on the free table, same limits as the 120b. The "trains on your data" problem that disqualifies Gemini and Mistral for a public project simply doesn't exist here.

**2. Google Gemini — best fallback, but check the training clause.** Generous free tier, no card, excellent Flash-Lite latency, and the broadest free model list. **The blocker for a public project is that free-tier content is explicitly "used to improve our products."** If your narration prompts include user data, this is disqualifying unless you pay. If prompts are purely game metadata (public information anyway), the risk is low and Gemini is very attractive. Deduct for the fact that you now cannot read your rate limits from the docs at all.

**3. OpenRouter — best for prototyping and resilience, poor for volume.** No card, an OpenAI drop-in, and 20 free models with automatic failover across providers. But **50 requests/day at zero spend** makes it unusable for catalogue-scale work; a one-time $10 unlocks 1,000/day, which changes the calculus entirely. Also the least predictable: the `:free` roster churns, and per-model latency varies by upstream provider. Good as a **secondary/fallback route**, not a primary.

**4. Mistral — usable, but two unknowns.** Ministral 3 3B/8B would be well-suited on paper. Downgraded because **free-tier data is used for training by default** (opt-out exists but is off by default — the worst default of the five), and because the free limits are unpublished, so you cannot capacity-plan before signing up. Partial OpenAI compatibility adds integration friction.

**5. Cerebras — disqualified on the brief.** It fails the explicit "no card required" test: API access stays **inactive** until you add a verified payment method, and the $5 credits expire in 30 days with no always-free tier behind them. **5 RPM** is also marginal for "a handful of requests per minute". Its inference speed is genuinely excellent, so it's a strong *paid* option — just not a free-tier one.

**On the sub-2s latency target: [unverified] for all five.** I had no API keys and could not measure end-to-end latency from this VM. Rankings on speed rest on vendor architecture claims and model size, not measurement — and Cerebras' own [pricing page](https://www.cerebras.ai/pricing) footnote concedes speed claims rest on *"third-party benchmarking or internal testing"* and *"may vary depending on workload, configuration, date and models being tested."* **Benchmark your top two from the actual VM before committing.** For a 150-token output, time-to-first-token and per-token rate both matter; prefer streaming so perceived latency is TTFT rather than total.

**Practical recommendation:** primary **Groq** (`openai/gpt-oss-20b`), fallback **Gemini Flash-Lite** if the training clause is acceptable for your prompt content. Both are OpenAI-compatible, so a single client with a swapped `base_url` covers both and keeps the fallback cheap to implement.

---

## Embedding models

Both verified from the Hugging Face model cards and the HF API (`?blobs=true`) for exact on-disk sizes, 2026-09-17.

| | **BAAI/bge-small-en-v1.5** | **sentence-transformers/all-MiniLM-L6-v2** |
|---|---|---|
| Embedding dimension | **384** | **384** |
| Parameters | 33,360,512 (33.4M) | 22,713,728 (22.7M) |
| Size on disk | **133.5 MB** (`model.safetensors`); ONNX 133.1 MB | **90.9 MB** (`model.safetensors`); ONNX 90.4 MB; **int8 ONNX 23.0 MB** |
| Max sequence length | **512** (`sentence_bert_config.json`: `max_seq_length: 512`; `max_position_embeddings: 512`) | **256** (`sentence_bert_config.json`: `max_seq_length: 256`) — despite `max_position_embeddings: 512` |
| Layers / hidden | 12 layers, hidden 384 | 6 layers, hidden 384 |
| Asymmetric query prefix | **Optional** — see below | **None** |
| Licence | **MIT** | **Apache 2.0** |

Sources: [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5), [sentence-transformers/all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2), plus `config.json` / `sentence_bert_config.json` in each repo.

### Query prefix

**bge-small-en-v1.5 — optional, not required.** The card's guidance, verbatim: *"If you need to search the relevant passages to a query, we suggest to add the instruction to the query"*, and for v1.5 specifically *"we improve its retrieval ability when not using instruction. No instruction only has a slight degradation in retrieval performance compared with using instruction."* The English prefix is:

```
Represent this sentence for searching relevant passages:
```

Apply it **only to queries, never to passages** — *"no instruction"* is ever added to passages. The card advises *"For a retrieval task that uses short queries to find long related documents, it is recommended to add instructions for these short queries"*, and ultimately to choose *"the setting that achieves better performance on your task"*.

**all-MiniLM-L6-v2 — symmetric, no prefix.** The card's usage examples encode raw sentences with no instruction, then mean-pool over token embeddings (attention-mask-weighted) and L2-normalise.

### MTEB scores

**bge-small-en-v1.5** — from the model card's own table: **Retrieval (15 tasks): 51.68**; Average (56 tasks): 62.17. Also Classification 74.14, Clustering 43.82, Pair Classification 84.92, Reranking 58.36, STS 81.59, Summarization 30.12.

**all-MiniLM-L6-v2 — [unverified].** The model card **reports no MTEB scores at all** (the model predates MTEB). The [sentence-transformers pretrained models page](https://sbert.net/docs/sentence_transformer/pretrained_models.html) also gives no MTEB figure for it — only the relative note that all-mpnet-base-v2 *"provides the best quality"* while all-MiniLM-L6-v2 *"is 5 times faster and still offers good quality."* The commonly cited MTEB retrieval figure of ~41.95 is **not** on any first-party page I could reach; the live MTEB leaderboard is JS-rendered and was not machine-readable here. **Treat any specific MiniLM MTEB number as unconfirmed.**

The safe first-party comparison: bge-small-en-v1.5 has a published retrieval score, is a 12-layer model at 1.5× the size and 2× the usable sequence length, and is the stronger retrieval model — but MiniLM is ~1.5× smaller, 6 layers (so roughly 2× faster), and has a well-tested int8 ONNX at 23 MB.

### Licence constraints

Neither is restrictive. **bge-small-en-v1.5 is MIT** and the card explicitly notes released models *"can be used for commercial purposes free of charge."* **all-MiniLM-L6-v2 is Apache 2.0.**

One thing worth flagging for a public project: all-MiniLM-L6-v2 was fine-tuned on **1,170,060,424 sentence pairs**, dominated by **Reddit comments 2015–2018 (~726M)**, S2ORC citation pairs (~116M), WikiAnswers (~77M) and PAQ (~64M). The Apache-2.0 licence covers the model weights; it does not resolve the provenance of that training corpus. Not a practical blocker, but relevant if you need to document data lineage.

### Recommendation for a game-description index

**bge-small-en-v1.5**, for three reasons specific to this use case: (1) **512-token sequences vs 256** matters because Steam `short_description` plus genre/category text routinely exceeds 256 word-pieces and MiniLM would silently truncate; (2) it has a **published MTEB retrieval score (51.68)** where MiniLM has none; (3) MIT licensing. The cost is 133 MB vs 91 MB and roughly half the throughput. If you're embedding only `short_description` (which is short) and throughput dominates, MiniLM with the int8 ONNX (23 MB) is the pragmatic choice. Both output 384-dim vectors, so the index format is identical and **you can switch later without changing your vector store schema** — just re-embed.

---

## Everything marked [unverified], collected

| Claim | Why unverified | Best available evidence |
|---|---|---|
| `GetAppList/v2` historical response shape | Endpoint is dead; cannot observe | Analogous `GetPartnerAppListForWebAPIKey` schema in Valve docs |
| `IStoreService/GetAppList` actual behaviour | No API key in this environment | Valve's parameter table; confirmed 403 (exists, needs key) |
| `appdetails` `type` values beyond game/dlc/music | No Valve enumeration exists | `include_*` flags in IStoreService docs imply software/video/hardware |
| `content_descriptors.ids` numeric mapping | Valve publishes no mapping | Community mapping consistent with the `notes` string on appid 1091500 |
| Cause of `appdetails` `success:false` (appid 1229230) | Could not isolate age-gate vs region vs delisting; birthtime cookie didn't help | Reproducible `success:false`, no data key |
| Steam limiter internals (token bucket, exact params) | Valve documents nothing | Measured burst 247, sustained 208/5min, ~16s recovery, alternating open/closed windows |
| Whether `appdetails` and `appreviews` share a rate bucket | Budget refilled before the probe | `appreviews` absorbed 60 rapid reqs while `appdetails` was healthy — inconclusive |
| GameDataCrunch reliability/licence | Not tested | Third-party README mention only |
| Gemini free-tier RPM/TPM/RPD numbers | Google removed the table; AI Studio is login-gated | Docs confirm the RPM/TPM/RPD dimensions, per-project scope, midnight-PT reset |
| Groq free tier "no credit card" | No explicit first-party statement found | Documented Free Plan with concrete limits; quickstart never mentions billing |
| Cerebras "does not train on inputs" | No explicit non-training clause found | Privacy Policy: *"We do not retain inputs and outputs"* |
| OpenRouter free endpoints publishing prompts | Not stated in docs | Docs describe an opt-out toggle, separate settings for free vs paid |
| Mistral free-tier numeric limits | Mistral removed published numbers | Limit *categories* documented; "lowest limits" for Free mode |
| Mistral card/phone requirement | Not addressed in docs | Free mode described only as "the default" |
| Mistral current API model id strings | Docs list ids only for deprecated models | Model names + Apache-2.0 licensing confirmed |
| Mistral OpenAI-compatible base URL | Not confirmed first-party | Native SDK/REST is the documented path |
| Sub-2s latency for all five providers | No API keys; could not measure | Vendor architecture claims only; Cerebras concedes benchmarks "may vary" |
| all-MiniLM-L6-v2 MTEB retrieval score | Not on the model card or sbert.net | Only a relative claim: "5 times faster and still offers good quality" |
