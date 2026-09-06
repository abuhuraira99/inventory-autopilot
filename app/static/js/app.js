/* ==========================================================================
   Dashboard behaviour.
   ==========================================================================

   Deliberately small and dependency-free. Every page works without JavaScript:
   forms are real form posts, links are real links, and nothing here is load-
   bearing. This adds convenience on top, which matters for a system whose job
   is to keep working unattended.

   What it does:
     1. remembers the light/dark choice
     2. refreshes the status tiles every 20 seconds, without a page reload
     3. dismisses the snackbar, and cleans the flash message out of the URL
     4. guards the typed confirmations (UNDO, INITIAL SYNC)
     5. warns before leaving a settings form with unsaved edits
     6. filters long tables client-side
   ========================================================================== */

(function () {
  "use strict";

  /* ======================================================================
     1. Theme
     ======================================================================
     Three states, matching the CSS: "light", "dark", and absent (follow the
     operating system). Absent is the default, which is why the toggle cycles
     through all three rather than flipping between two. */

  var THEME_KEY = "autopilot-theme";

  function applyTheme(value) {
    if (value === "light" || value === "dark") {
      document.documentElement.setAttribute("data-theme", value);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  function storedTheme() {
    try {
      return localStorage.getItem(THEME_KEY);
    } catch (e) {
      // Private browsing, or site data blocked. Follow the OS.
      return null;
    }
  }

  function cycleTheme() {
    var order = [null, "light", "dark"];
    var current = storedTheme();
    var next = order[(order.indexOf(current) + 1) % order.length];
    try {
      if (next === null) {
        localStorage.removeItem(THEME_KEY);
      } else {
        localStorage.setItem(THEME_KEY, next);
      }
    } catch (e) {
      /* not fatal - the choice just will not persist */
    }
    applyTheme(next);
    updateThemeButton(next);
  }

  function updateThemeButton(value) {
    var btn = document.querySelector("[data-theme-toggle]");
    if (!btn) return;
    var label = value === "light" ? "Light" : value === "dark" ? "Dark" : "Auto";
    var icon = value === "light" ? "☀" : value === "dark" ? "☽" : "◑";
    btn.textContent = icon;
    btn.setAttribute("title", "Appearance: " + label + " (click to change)");
    btn.setAttribute("aria-label", "Appearance: " + label);
  }

  // Applied before first paint by an inline script in base.html, so there is
  // no flash of the wrong theme. This only wires up the button.
  applyTheme(storedTheme());

  /* ======================================================================
     2. Live status
     ======================================================================
     Polls /api/status and updates any element carrying data-live="<path>".
     Twenty seconds is frequent enough that the page never looks stale and
     infrequent enough to be invisible in the logs.

     Polling rather than websockets on purpose: one endpoint, no connection
     state to manage, and it works through any proxy or tunnel. */

  var POLL_MS = 20000;
  var pollTimer = null;
  var pollFailures = 0;

  function readPath(object, path) {
    return path.split(".").reduce(function (node, key) {
      return node == null ? null : node[key];
    }, object);
  }

  function formatNumber(value) {
    if (value === null || value === undefined || value === "") return "—";
    var n = Number(value);
    return isNaN(n) ? String(value) : n.toLocaleString();
  }

  function refreshStatus() {
    fetch("/api/status", { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (response) {
        if (response.status === 307 || response.status === 401) {
          // The session expired. Reload so the login page is shown rather
          // than leaving a dead dashboard on screen.
          window.location.reload();
          throw new Error("signed out");
        }
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (data) {
        pollFailures = 0;

        document.querySelectorAll("[data-live]").forEach(function (element) {
          var value = readPath(data, element.getAttribute("data-live"));
          if (value === null || value === undefined) return;
          element.textContent = element.hasAttribute("data-live-raw")
            ? String(value)
            : formatNumber(value);
        });

        // A run in progress gets a moving bar, so a long full-feed run does
        // not look like a hung page.
        var running = data.last_run && data.last_run.status === "running";
        document.querySelectorAll("[data-live-running]").forEach(function (element) {
          element.hidden = !running;
        });

        var stale = document.querySelector("[data-live-stale]");
        if (stale) stale.hidden = true;
      })
      .catch(function () {
        pollFailures += 1;
        // After three consecutive failures, say so rather than silently
        // showing numbers that may be minutes old.
        if (pollFailures >= 3) {
          var stale = document.querySelector("[data-live-stale]");
          if (stale) stale.hidden = false;
        }
      });
  }

  function startPolling() {
    if (!document.querySelector("[data-live]")) return;
    refreshStatus();
    pollTimer = window.setInterval(refreshStatus, POLL_MS);
  }

  // Stop polling while the tab is hidden; refresh at once on return.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (pollTimer) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    } else if (!pollTimer && document.querySelector("[data-live]")) {
      refreshStatus();
      pollTimer = window.setInterval(refreshStatus, POLL_MS);
    }
  });

  /* ======================================================================
     3. Snackbar
     ====================================================================== */

  function initSnackbar() {
    var bar = document.querySelector(".md-snackbar");
    if (!bar) return;

    // The flash travels in the query string. Strip it so a refresh does not
    // show the same message again, and so a bookmarked URL stays clean.
    if (window.history && window.history.replaceState) {
      var url = new URL(window.location.href);
      if (url.searchParams.has("flash")) {
        url.searchParams.delete("flash");
        url.searchParams.delete("tone");
        window.history.replaceState({}, "", url.toString());
      }
    }

    var close = bar.querySelector(".md-snackbar__close");
    if (close) {
      close.addEventListener("click", function () {
        bar.remove();
      });
    }

    // Errors and warnings stay until dismissed; a success message is
    // transient, because it needs no action.
    if (bar.classList.contains("md-snackbar--good")) {
      window.setTimeout(function () {
        bar.style.transition = "opacity 300ms";
        bar.style.opacity = "0";
        window.setTimeout(function () {
          bar.remove();
        }, 320);
      }, 6000);
    }
  }

  /* ======================================================================
     4. Typed confirmations
     ======================================================================
     Undo and the initial full sync require a word to be typed. The submit
     button stays disabled until it matches, so the friction is visible rather
     than being an error after the fact. */

  function initConfirmations() {
    document.querySelectorAll("[data-confirm-word]").forEach(function (form) {
      var word = form.getAttribute("data-confirm-word").toUpperCase();
      var input = form.querySelector("[data-confirm-input]");
      var button = form.querySelector("[data-confirm-submit]");
      if (!input || !button) return;

      function check() {
        var ok = input.value.trim().toUpperCase() === word;
        button.disabled = !ok;
        button.setAttribute("aria-disabled", String(!ok));
      }

      input.addEventListener("input", check);
      check();
    });

    // A plain one-click confirmation for the rest.
    document.querySelectorAll("[data-confirm]").forEach(function (element) {
      element.addEventListener("click", function (event) {
        if (!window.confirm(element.getAttribute("data-confirm"))) {
          event.preventDefault();
          event.stopPropagation();
        }
      });
    });
  }

  /* ======================================================================
     5. Unsaved settings
     ======================================================================
     The settings page is long. Losing edits by navigating away would be a
     real annoyance for the person configuring safety limits. */

  function initDirtyGuard() {
    var form = document.querySelector("[data-dirty-guard]");
    if (!form) return;

    var dirty = false;

    form.addEventListener("change", function () {
      dirty = true;
      var indicator = document.querySelector("[data-dirty-indicator]");
      if (indicator) indicator.hidden = false;
    });

    form.addEventListener("submit", function () {
      dirty = false;
    });

    window.addEventListener("beforeunload", function (event) {
      if (!dirty) return undefined;
      event.preventDefault();
      // Browsers show their own wording; the return value only signals intent.
      event.returnValue = "";
      return "";
    });
  }

  /* ======================================================================
     6. Table filter
     ======================================================================
     Client-side, over rows already on the page. Server-side search is a
     separate feature on /products; this is for narrowing what you can already
     see, which is what you want on a 500-row run detail. */

  function initTableFilter() {
    document.querySelectorAll("[data-filter-table]").forEach(function (input) {
      var table = document.querySelector(input.getAttribute("data-filter-table"));
      if (!table) return;
      var counter = document.querySelector(input.getAttribute("data-filter-count") || " ");

      input.addEventListener("input", function () {
        var term = input.value.trim().toLowerCase();
        var shown = 0;
        table.querySelectorAll("tbody tr").forEach(function (row) {
          var match = !term || row.textContent.toLowerCase().indexOf(term) !== -1;
          row.hidden = !match;
          if (match) shown += 1;
        });
        if (counter) counter.textContent = shown.toLocaleString();
      });
    });
  }

  /* ======================================================================
     7. Wire up
     ====================================================================== */

  function init() {
    var toggle = document.querySelector("[data-theme-toggle]");
    if (toggle) {
      toggle.addEventListener("click", cycleTheme);
      updateThemeButton(storedTheme());
    }

    initSnackbar();
    initConfirmations();
    initDirtyGuard();
    initTableFilter();
    startPolling();

    // Switches that submit their form the moment they are flipped: the pause
    // control and the mode selector. Waiting for a Save click on an emergency
    // stop would be the wrong design.
    document.querySelectorAll("[data-submit-on-change]").forEach(function (element) {
      element.addEventListener("change", function () {
        var form = element.closest("form");
        if (form) form.submit();
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
