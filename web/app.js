"use strict";

// Every value that reaches the DOM goes through textContent or a property assignment. Result names
// and descriptions are Steam's text, stored in the corpus and rendered back out here, so building
// this page with innerHTML would make the ingest a route to script injection.

const form = document.getElementById("query");
const submit = document.getElementById("submit");
const statusLine = document.getElementById("status");
const narration = document.getElementById("narration");
const results = document.getElementById("results");
const corpus = document.getElementById("corpus");

// The API answers 503 with Retry-After while MinDB restarts, which is a normal event on a
// single-instance Recreate deployment rather than an incident (D7). Retrying it here means a rollout
// looks like a pause to whoever is on the page, which is what the header is for.
const MAX_RETRIES = 2;

function setStatus(text, state) {
  statusLine.textContent = text;
  statusLine.hidden = !text;
  if (state) {
    statusLine.dataset.state = state;
  } else {
    delete statusLine.dataset.state;
  }
}

function sleep(seconds) {
  return new Promise((resolve) => setTimeout(resolve, seconds * 1000));
}

async function showCorpusSize() {
  try {
    const response = await fetch("/health");
    if (!response.ok) throw new Error(String(response.status));
    const body = await response.json();
    if (body.corpus_size === 0) {
      corpus.dataset.state = "empty";
      corpus.textContent =
        "The corpus is empty — nothing has been ingested yet, so every query returns nothing.";
      return;
    }
    corpus.dataset.state = "ready";
    corpus.textContent =
      `${body.corpus_size.toLocaleString()} games indexed · ${body.mindb.kernel} kernel`;
  } catch {
    corpus.dataset.state = "down";
    corpus.textContent = "The service is not answering /health.";
  }
}

function card(hit, topScore) {
  const element = document.createElement("article");
  element.className = "card";

  const heading = document.createElement("h2");
  if (hit.appid) {
    const link = document.createElement("a");
    link.href = `https://store.steampowered.com/app/${encodeURIComponent(hit.appid)}/`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = hit.name;
    heading.append(link);
  } else {
    heading.textContent = hit.name;
  }
  element.append(heading);

  if (hit.short_description) {
    const description = document.createElement("p");
    description.textContent = hit.short_description;
    element.append(description);
  }

  const meta = document.createElement("div");
  meta.className = "meta";

  const bar = document.createElement("div");
  bar.className = "bar";
  const fill = document.createElement("span");
  // Relative to the best score in this response, not to 1.0. A cosine of 0.72 means nothing on its
  // own; what a reader can use is how far this one is behind the top pick.
  const share = topScore > 0 ? Math.max(0, hit.score) / topScore : 0;
  fill.style.width = `${(share * 100).toFixed(1)}%`;
  bar.append(fill);
  meta.append(bar);

  const score = document.createElement("span");
  score.textContent = hit.score.toFixed(4);
  meta.append(score);

  if (hit.genres && hit.genres.length) {
    const genres = document.createElement("div");
    genres.className = "genres";
    for (const name of hit.genres) {
      const chip = document.createElement("span");
      chip.className = "genre";
      chip.textContent = name;
      genres.append(chip);
    }
    meta.append(genres);
  }

  element.append(meta);
  return element;
}

function render(body) {
  results.replaceChildren();
  narration.hidden = true;
  narration.textContent = "";

  if (!body.results.length) {
    setStatus("No matches. Try a looser description, or a different seed game.");
    return;
  }

  if (body.narration) {
    narration.textContent = body.narration;
    narration.hidden = false;
  }

  const topScore = body.results[0].score;
  for (const hit of body.results) {
    results.append(card(hit, topScore));
  }

  const seed = body.seed ? ` like ${body.seed.name}` : "";
  setStatus(`${body.results.length} result${body.results.length === 1 ? "" : "s"}${seed}.`);
}

async function ask(url, attempt = 0) {
  const response = await fetch(url);

  if (response.status === 503 && attempt < MAX_RETRIES) {
    const wait = Number(response.headers.get("Retry-After")) || 5;
    for (let left = wait; left > 0; left--) {
      setStatus(`The index is restarting. Retrying in ${left}s…`);
      await sleep(1);
    }
    return ask(url, attempt + 1);
  }

  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    // FastAPI puts the message in `detail`, and for a seed miss that message is the one worth
    // reading: the name index explains why a game it knows about is not recommendable (D11).
    throw new Error(body.detail || `The service answered ${response.status}.`);
  }
  return body;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const q = document.getElementById("q").value.trim();
  const seed = document.getElementById("seed").value.trim();
  if (!q && !seed) {
    setStatus("Give a mood, a seed game, or both.", "error");
    return;
  }

  const params = new URLSearchParams({
    k: document.getElementById("k").value,
    narrate: document.getElementById("narrate").checked ? "true" : "false",
  });
  if (q) params.set("q", q);
  if (seed) params.set("seed", seed);

  submit.disabled = true;
  setStatus("Searching…");
  results.replaceChildren();
  narration.hidden = true;

  try {
    render(await ask(`/recommend?${params}`));
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    submit.disabled = false;
  }
});

// Ctrl+Enter from the mood box, because Enter inside a textarea is a newline and a one-field form
// wants a keyboard submit.
document.getElementById("q").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    form.requestSubmit();
  }
});

showCorpusSize();
