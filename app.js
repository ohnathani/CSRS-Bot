"use strict";

const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const selectionStorageKey = "csrs-enlistment-selection-v1";
const appShell = document.querySelector("main");
const statusElement = document.getElementById("app-status");
const loginPanel = document.getElementById("login-panel");
const selectionPanel = document.getElementById("selection-panel");
const reviewPanel = document.getElementById("review-panel");
const resultsPanel = document.getElementById("results-panel");
const loginForm = document.getElementById("login-form");
const passwordInput = document.getElementById("password");
const loginButton = document.getElementById("login-button");
const saveButton = document.getElementById("save-selection");
const selectionStatus = document.getElementById("selection-status");
const reviewList = document.getElementById("review-list");
const resultList = document.getElementById("result-list");

let tables = [];
let subjectsByKey = new Map();
let selectedKeys = new Set();
let savedKeys = new Set();

function setStatus(element, message, isError = false) {
  element.textContent = message;
  element.classList.toggle("error", isError);
}

async function postJson(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify(payload),
  });
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error("The local app returned an unreadable response.");
  }
  if (!response.ok) {
    throw new Error(result.error || `Request failed (${response.status}).`);
  }
  return result;
}

function readSavedKeys() {
  try {
    const stored = localStorage.getItem(selectionStorageKey);
    if (stored === null) {
      return new Set();
    }
    const values = JSON.parse(stored);
    if (!Array.isArray(values) || values.some((value) => typeof value !== "string")) {
      throw new Error("Saved selection has an invalid format.");
    }
    return new Set(values);
  } catch (error) {
    setStatus(
      selectionStatus,
      `Could not read saved selections: ${error.message}`,
      true,
    );
    return new Set();
  }
}

function persistSelectedKeys() {
  try {
    localStorage.setItem(selectionStorageKey, JSON.stringify([...selectedKeys]));
    return true;
  } catch (error) {
    setStatus(
      selectionStatus,
      `Could not save selections: ${error.message}`,
      true,
    );
    return false;
  }
}

function subjectLabel(subject) {
  const details = subject.cells.slice(0, 4).filter(Boolean).join(" | ");
  return `${subject.category}: ${details}`;
}

function setPanels({ login, selection, review, results }) {
  loginPanel.hidden = !login;
  selectionPanel.hidden = !selection;
  reviewPanel.hidden = !review;
  resultsPanel.hidden = !results;
  appShell.classList.toggle(
    "app-shell--wide",
    selection || review || results,
  );
}

function buildTable(table, category) {
  const wrapper = document.createElement("div");
  wrapper.className = "table-wrap";
  const element = document.createElement("table");
  const head = document.createElement("thead");
  const headerRow = document.createElement("tr");
  const selectHeader = document.createElement("th");
  selectHeader.className = "select-cell";
  selectHeader.scope = "col";
  selectHeader.textContent = "Select";
  headerRow.append(selectHeader);

  table.headers.forEach((header) => {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = header;
    headerRow.append(cell);
  });
  head.append(headerRow);
  element.append(head);

  const body = document.createElement("tbody");
  table.subjects.forEach((subject) => {
    const row = document.createElement("tr");
    const selectCell = document.createElement("td");
    selectCell.className = "select-cell";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selectedKeys.has(subject.key);
    checkbox.disabled = !subject.selectable;
    checkbox.setAttribute("aria-label", `Select ${subjectLabel({
      ...subject,
      category,
    })}`);
    if (subject.reason) {
      checkbox.title = subject.reason;
    }
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) {
        selectedKeys.add(subject.key);
      } else {
        selectedKeys.delete(subject.key);
      }
      saveButton.disabled = selectedKeys.size === 0;
      reviewPanel.hidden = true;
      if (persistSelectedKeys()) {
        setStatus(selectionStatus, "Selection changes saved.");
      }
    });

    selectCell.append(checkbox);
    if (!subject.selectable) {
      const reason = document.createElement("span");
      reason.className = "unavailable";
      reason.textContent = subject.reason || "Unavailable";
      selectCell.append(document.createElement("br"), reason);
    }
    row.append(selectCell);

    subject.cells.forEach((value, cellIndex) => {
      const cell = document.createElement("td");
      const isAddColumn =
        table.headers[cellIndex]?.trim().toLowerCase() === "add";
      cell.textContent =
        !value && isAddColumn && subject.action_label
          ? subject.action_label
          : value;
      row.append(cell);
    });
    body.append(row);
  });

  element.append(body);
  wrapper.append(element);
  return wrapper;
}

function renderTables(responseTables) {
  tables = responseTables;
  subjectsByKey = new Map();
  const availableKeys = new Set();
  tables.forEach((table) => {
    table.subjects.forEach((subject) => {
      subjectsByKey.set(subject.key, {
        ...subject,
        category: table.category,
      });
      if (subject.selectable) {
        availableKeys.add(subject.key);
      }
    });
  });
  selectedKeys = new Set(
    [...readSavedKeys()].filter((key) => availableKeys.has(key)),
  );
  savedKeys = new Set(selectedKeys);

  const containers = {
    PE: document.getElementById("pe-tables"),
    NSTP: document.getElementById("nstp-tables"),
  };
  Object.values(containers).forEach((container) => {
    container.replaceChildren();
  });

  tables.forEach((table) => {
    const category = table.category;
    const container = containers[category];
    if (!container) {
      return;
    }
    const visibleTable =
      category === "NSTP"
        ? {
            ...table,
            subjects: table.subjects.filter((subject) => subject.selectable),
          }
        : table;
    if (visibleTable.subjects.length > 0) {
      container.append(buildTable(visibleTable, category));
    }
  });

  for (const [category, container] of Object.entries(containers)) {
    if (container.childElementCount === 0) {
      const empty = document.createElement("p");
      empty.className = "muted";
      const hasLiveTable = tables.some((table) => table.category === category);
      empty.textContent =
        category === "NSTP" && hasLiveTable
          ? "No available NSTP subjects."
          : `No live ${category} table was returned.`;
      container.append(empty);
    }
  }

  persistSelectedKeys();

  saveButton.disabled = selectedKeys.size === 0;
  setPanels({ login: false, selection: true, review: false, results: false });
}

function renderReview() {
  reviewList.replaceChildren();
  for (const key of savedKeys) {
    const subject = subjectsByKey.get(key);
    if (!subject) {
      continue;
    }
    const item = document.createElement("li");
    item.textContent = subjectLabel(subject);
    reviewList.append(item);
  }
  setPanels({ login: false, selection: false, review: true, results: false });
}

function renderResults(results) {
  const labels = {
    success: "Successfully enlisted",
    already_enlisted: "Already enlisted",
    failed: "Enlistment failed",
    unconfirmed: "Unconfirmed",
    session_lost: "Login session lost",
    not_attempted: "Not attempted",
  };
  resultList.replaceChildren();
  results.forEach((result) => {
    const item = document.createElement("li");
    item.dataset.status = result.status;
    const title = document.createElement("strong");
    const subject = subjectsByKey.get(result.key);
    title.textContent = `${labels[result.status] || "Result"}: ${
      subject ? subjectLabel(subject) : result.category
    }`;
    const message = document.createElement("p");
    message.textContent = result.message;
    item.append(title, message);
    resultList.append(item);
  });
  setPanels({ login: false, selection: false, review: false, results: true });
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!loginForm.reportValidity()) {
    return;
  }

  loginButton.disabled = true;
  setStatus(statusElement, "Logging in and loading live PE/NSTP tables...");
  try {
    const response = await postJson("/api/login", {
      username: document.getElementById("username").value,
      password: passwordInput.value,
    });
    passwordInput.value = "";
    renderTables(response.tables);
    setStatus(statusElement, "Live Registration subjects loaded.");
  } catch (error) {
    passwordInput.value = "";
    setStatus(statusElement, error.message, true);
  } finally {
    loginButton.disabled = false;
  }
});

saveButton.addEventListener("click", () => {
  if (selectedKeys.size === 0) {
    setStatus(selectionStatus, "Select at least one available subject.", true);
    return;
  }
  if (!persistSelectedKeys()) {
    return;
  }
  savedKeys = new Set(selectedKeys);
  setStatus(selectionStatus, `${savedKeys.size} subject selection(s) saved.`);
  renderReview();
});

document.getElementById("edit-selection").addEventListener("click", () => {
  selectedKeys = new Set(savedKeys);
  setPanels({ login: false, selection: true, review: false, results: false });
  setStatus(selectionStatus, "Change the checkboxes, then save again.");
});

document.getElementById("confirm-enlist").addEventListener("click", async (event) => {
  if (savedKeys.size === 0) {
    setStatus(statusElement, "Save at least one subject before confirming.", true);
    return;
  }
  const button = event.currentTarget;
  button.disabled = true;
  setStatus(statusElement, "Submitting confirmed subjects to the portal...");
  try {
    const response = await postJson("/api/enlist", {
      keys: [...savedKeys],
      confirmed: true,
    });
    renderResults(response.results);
    setStatus(statusElement, "The portal response for each selected subject is shown below.");
  } catch (error) {
    setStatus(statusElement, error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.getElementById("logout-button").addEventListener("click", async () => {
  try {
    await postJson("/api/logout", {});
    tables = [];
    subjectsByKey.clear();
    passwordInput.value = "";
    setPanels({ login: true, selection: false, review: false, results: false });
    setStatus(statusElement, "Selenium session closed. Saved subject selections remain.");
  } catch (error) {
    setStatus(statusElement, error.message, true);
  }
});

document.getElementById("close-session").addEventListener("click", async () => {
  try {
    await postJson("/api/logout", {});
    tables = [];
    subjectsByKey.clear();
    passwordInput.value = "";
    setPanels({ login: true, selection: false, review: false, results: false });
    setStatus(statusElement, "Selenium session closed.");
  } catch (error) {
    setStatus(statusElement, error.message, true);
  }
});

