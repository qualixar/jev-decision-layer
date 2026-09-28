"use strict";

const csrf = document.querySelector('meta[name="jev-csrf"]').content;
const list = document.getElementById("recipe-list");
const panel = document.getElementById("detail-panel");
const search = document.getElementById("recipe-search");
const count = document.getElementById("recipe-count");
const recipeTotal = document.getElementById("recipe-total");
let recipes = [];
let selected = null;
let currentValues = {};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

async function request(path, payload) {
  const headers = {};
  const options = { credentials: "same-origin", headers };
  if (payload !== undefined) {
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = csrf;
    options.method = "POST";
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message || "The request could not be completed.");
  return body;
}

function humanize(value) {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function renderList() {
  const query = search.value.trim().toLowerCase();
  const filtered = recipes.filter((recipe) => `${recipe.title} ${recipe.audience} ${recipe.id}`.toLowerCase().includes(query));
  count.textContent = `${filtered.length} shown`;
  list.replaceChildren();
  const featured = filtered.filter((recipe) => recipe.featured_for);
  const rest = filtered.filter((recipe) => !recipe.featured_for);
  if (featured.length) {
    list.append(node("div", "group-label", "Good starting points"));
    featured.forEach((recipe) => list.append(recipeButton(recipe)));
  }
  if (rest.length) {
    list.append(node("div", "group-label", query ? "Matching recipes" : "Browse all recipes"));
    rest.forEach((recipe) => list.append(recipeButton(recipe)));
  }
  if (!filtered.length) list.append(node("p", "mode-note", "No recipes match that search. Try a broader word."));
}

function recipeButton(recipe) {
  const button = node("button", "recipe-card");
  button.type = "button";
  button.setAttribute("aria-current", String(selected?.id === recipe.id));
  button.append(node("strong", "", recipe.title));
  button.append(node("small", "", recipe.audience));
  if (recipe.featured_for) button.append(node("span", "tag", `For ${recipe.featured_for}`));
  button.addEventListener("click", () => {
    selected = recipe;
    currentValues = {};
    renderList();
    renderDetail();
  });
  return button;
}

function renderDetail() {
  if (!selected) return;
  panel.replaceChildren();
  const head = node("div", "detail-head");
  const heading = node("div");
  heading.append(node("div", "eyebrow", selected.featured_for ? `START HERE · ${selected.featured_for.toUpperCase()}` : "DECISION RECIPE"));
  heading.append(node("h2", "", selected.title));
  heading.append(node("p", "", `Recipe ${selected.id} · audience: ${selected.audience}`));
  head.append(heading);
  head.append(node("span", "status-chip", "Specification · not model evaluated"));
  panel.append(head);

  panel.append(node("div", "section-label", "When this helps"));
  const explainer = node("div", "explain-box");
  explainer.append(node("p", "", selected.questions?.decision?.instructions || "A bounded decision checked by a local gate."));
  explainer.append(node("p", "limitations", `Limit: ${selected.limitations}`));
  panel.append(explainer);

  panel.append(node("div", "section-label", "Learn with a synthetic example"));
  const note = node("div", "mode-note", "This replays a hand-written example through the actual local gate. It makes no provider call and does not show model accuracy.");
  panel.append(note);
  const examples = node("div", "button-row");
  ["nominal", "uncertain", "adversarial"].forEach((variant) => {
    const button = node("button", "button", humanize(variant));
    button.type = "button";
    button.addEventListener("click", () => runFixture(variant));
    examples.append(button);
  });
  panel.append(examples);

  panel.append(node("div", "section-label", "Try one request with my own text"));
  panel.append(node("div", "mode-note", "The next step shows the exact route, data scope, byte estimate and local receipt behavior before any provider call."));
  panel.append(buildForm());
}

function buildForm() {
  const form = node("form", "field-grid");
  const schema = selected.input_schema;
  for (const key of schema.required) {
    const rule = schema.properties[key];
    const wrap = node("div", "field");
    const id = `field-${key}`;
    const label = node("label", "", humanize(key));
    label.htmlFor = id;
    wrap.append(label);
    wrap.append(node("span", "hint", `Required · up to ${rule.maxLength} characters`));
    const field = node("textarea");
    field.id = id;
    field.name = key;
    field.required = true;
    field.maxLength = rule.maxLength;
    field.value = currentValues[key] || "";
    field.setAttribute("aria-describedby", `${id}-hint`);
    wrap.lastChild.id = `${id}-hint`;
    field.addEventListener("input", () => { currentValues[key] = field.value; });
    wrap.append(field);
    form.append(wrap);
  }
  const scopeWrap = node("div", "field");
  const scopeLabel = node("label", "", "What kind of information is this?");
  scopeLabel.htmlFor = "data-scope";
  scopeWrap.append(scopeLabel);
  scopeWrap.append(node("span", "hint", "Choose the scope that matches the text you enter."));
  const scope = node("select");
  scope.id = "data-scope";
  scope.name = "data_classification";
  [["public", "Public information"], ["internal-minimized", "Internal, minimized"], ["restricted", "Restricted"]].forEach(([value, title]) => {
    const option = node("option", "", title);
    option.value = value;
    scope.append(option);
  });
  scopeWrap.append(scope);
  form.append(scopeWrap);

  const submit = node("button", "button primary", "Review this request");
  submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const values = {};
    schema.required.forEach((key) => { values[key] = form.elements.namedItem(key).value; });
    currentValues = values;
    reviewRequest(values, scope.value);
  });
  return form;
}

async function runFixture(variant) {
  clearNotice();
  try {
    const response = await request("/api/offline-example", { recipe_id: selected.id, variant });
    const outcome = response.outcome;
    const box = node("section", "result-box");
    box.setAttribute("aria-live", "polite");
    box.append(node("span", "result-status", "SYNTHETIC · OFFLINE"));
    box.append(node("h3", "", `${humanize(variant)} example ${outcome.passed ? "held" : "needs review"}`));
    box.append(node("p", "", response.disclaimer));
    box.append(node("p", "", `Expected: ${outcome.expected.host_action} · observed: ${outcome.observed?.host_action || "unavailable"}`));
    box.append(node("p", "", `Gate recommendation: ${outcome.observed?.recommendation || "not available"}`));
    panel.append(box);
    box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  } catch (error) { showNotice(error.message); }
}

async function reviewRequest(values, classification) {
  clearNotice();
  try {
    const response = await request("/api/review", { recipe_id: selected.id, values, data_classification: classification });
    showReview(response.review, response.review_nonce, values);
  } catch (error) { showNotice(error.message); }
}

function showReview(review, nonce, values) {
  removeById("live-result");
  removeById("review-card");
  const box = node("section", "review-box");
  box.id = "review-card";
  box.setAttribute("aria-labelledby", "review-heading");
  box.append(node("div", "eyebrow", "PAUSE AND CHECK"));
  const title = node("h3", "", "Review exactly what will happen");
  title.id = "review-heading";
  box.append(title);
  const grid = node("div", "review-grid");
  [["Route", `${review.route_kind} · ${review.provider}`], ["Model", review.model],
   ["Data scope", review.data_classification], ["Text leaves this device", review.text_leaves_device ? "Yes, to the hosted provider" : "No, local route"],
   ["Request size", `${review.estimated_request_bytes} of ${review.max_request_bytes} bytes`],
   ["Daily limits", `${review.max_calls_per_day} requests · ${review.max_bytes_per_day} bytes`],
   ["Local records", "Receipt and cache may be retained"]].forEach(([label, value]) => {
    const item = node("div", "review-item");
    item.append(node("small", "", label));
    item.append(node("strong", "", value));
    grid.append(item);
  });
  box.append(grid);
  const exact = node("details", "exact-values");
  exact.append(node("summary", "", "Show the exact text being reviewed"));
  exact.append(node("pre", "", JSON.stringify(values, null, 2)));
  box.append(exact);
  box.append(node("p", "mode-note", review.notice));
  box.append(node("p", "mode-note", "Jev returns advice and a gate result. It does not send, publish, assign work, execute a tool, or change files."));
  if (review.requires_external_scope_confirmation) {
    const label = node("label", "check-row");
    const check = node("input");
    check.type = "checkbox";
    check.id = "external-scope-confirmation";
    const text = node("span", "", "I understand that this restricted text will be sent to the hosted provider shown above.");
    label.append(check, text);
    box.append(label);
  }
  const actions = node("div", "button-row");
  const run = node("button", "button primary", "Confirm and run one request");
  run.type = "button";
  run.addEventListener("click", () => runReviewed(nonce, review.requires_external_scope_confirmation));
  actions.append(run);
  const edit = node("button", "button", "Edit or cancel");
  edit.type = "button";
  edit.addEventListener("click", () => box.remove());
  actions.append(edit);
  box.append(actions);
  panel.append(box);
  box.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

async function runReviewed(nonce, requiresExternal) {
  clearNotice();
  const confirmation = document.getElementById("external-scope-confirmation");
  if (requiresExternal && !confirmation?.checked) {
    showNotice("Please confirm the hosted data scope before running this request.");
    return;
  }
  const payload = { review_nonce: nonce, confirm: true };
  if (requiresExternal) payload.external_scope_confirmation = true;
  try {
    const result = await request("/api/run", payload);
    showResult(result);
  } catch (error) { showNotice(error.message); }
}

function showResult(result) {
  removeById("live-result");
  const box = node("section", "result-box");
  box.id = "live-result";
  box.setAttribute("aria-live", "polite");
  box.append(node("span", "result-status", "LIVE REQUEST · ADVISORY"));
  box.append(node("h3", "", `Gate result: ${humanize(result.host_action || "review")}`));
  box.append(node("p", "", `Provider: ${result.provider} · model: ${result.model} · calibration: ${result.calibration_status}`));
  box.append(node("p", "", `The model answer and the local gate are separate. ${result.reason || "Review the answer yourself before taking action."}`));
  box.append(node("p", "", "execution_authorized: false · You decide the next step."));
  box.append(node("div", "section-label", "Typed answer"));
  box.append(node("pre", "", JSON.stringify(result.answer, null, 2)));
  const receipts = node("div", "receipt-row");
  [["Decision receipt", result.receipt_id], ["Gate receipt", result.policy_receipt_id]].forEach(([label, id]) => {
    if (!id) return;
    const button = node("button", "button receipt-button", `${label} · ${id}`);
    button.type = "button";
    button.addEventListener("click", () => readReceipt(id));
    receipts.append(button);
  });
  box.append(receipts);
  box.append(node("p", "mode-note", "Manual next step: inspect the source material and decide whether to act, verify further, or ignore this recommendation."));
  panel.append(box);
  box.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

async function readReceipt(id) {
  try {
    const response = await request(`/api/receipt/${id}`);
    const detail = node("section", "result-box");
    detail.append(node("div", "eyebrow", "LOCAL RECEIPT · NO NEW PROVIDER CALL"));
    detail.append(node("h3", "", "Receipt details"));
    detail.append(node("pre", "", JSON.stringify(response, null, 2)));
    panel.append(detail);
    detail.scrollIntoView({ block: "nearest", behavior: "smooth" });
  } catch (error) { showNotice(error.message); }
}

function showNotice(message) {
  clearNotice();
  const alert = node("div", "alert", message);
  alert.id = "workbench-alert";
  alert.role = "alert";
  panel.prepend(alert);
  alert.focus?.();
}

function clearNotice() { removeById("workbench-alert"); }
function removeById(id) { document.getElementById(id)?.remove(); }

search.addEventListener("input", renderList);

(async function start() {
  try {
    const response = await request("/api/catalog");
    recipes = response.recipes;
    recipeTotal.textContent = `${recipes.length} DECISION RECIPES`;
    renderList();
    if (recipes.length) {
      const preferred = recipes.find((recipe) => recipe.id === "qualixar.meeting-action-routing");
      if (preferred) { selected = preferred; renderList(); renderDetail(); }
    }
  } catch (error) {
    count.textContent = "Unavailable";
    showNotice(error.message);
  }
})();
