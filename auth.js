// AFLFantasyWire — account + login + cross-device sync (self-contained).
//
// Loaded once from index.html. It does two jobs:
//   1. Renders the account UI (a floating button + a sign-in / account modal),
//      supporting email+password and Google Sign-In.
//   2. Mirrors the app's existing localStorage keys (My Team, watchlist,
//      preferences) to the server per logged-in account, so a user sees the
//      same team on any device.
//
// The React app is untouched: it keeps reading/writing localStorage exactly as
// before. This layer just persists those keys server-side when signed in.
(function () {
  "use strict";

  // The localStorage keys we sync. Values are stored/restored as raw strings —
  // the React app is responsible for parsing them, so we never interpret them.
  var SYNC_KEYS = [
    "afw_myteam",       // parsed My Team (JSON)
    "afw_myteam_raw",   // raw My Team text
    "afw_watchlist",    // tracked players (JSON)
    "afw_theme",        // "dark" | "light"
    "afw_syncwatch",    // "1" | "0"
    "afw_tab",          // last active tab
    "aflfw_seen_news",  // seen-news ids (JSON)
  ];
  var MTIME_KEY = "afw_sync_mtime";      // server epoch ms of last apply/push
  var RELOAD_FLAG = "afw_synced_once";   // per-session guard against reload loops

  var state = { user: null, googleClientId: "" };
  var lastSnapshot = null;   // JSON string of the last known synced-keys snapshot
  var pushTimer = null;

  // ── small helpers ─────────────────────────────────────────────────────────
  function api(path, opts) {
    opts = opts || {};
    opts.credentials = "same-origin";
    opts.headers = opts.headers || {};
    if (opts.body && typeof opts.body !== "string") {
      opts.body = JSON.stringify(opts.body);
      opts.headers["content-type"] = "application/json";
    }
    return fetch(path, opts).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, status: r.status, body: j }; },
        function () { return { ok: r.ok, status: r.status, body: {} }; });
    });
  }
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  function snapshot() {
    var o = {};
    for (var i = 0; i < SYNC_KEYS.length; i++) {
      var v = lsGet(SYNC_KEYS[i]);
      if (v !== null) o[SYNC_KEYS[i]] = v;
    }
    return o;
  }
  function applyBlob(data) {
    if (!data) return false;
    var changed = false;
    for (var i = 0; i < SYNC_KEYS.length; i++) {
      var k = SYNC_KEYS[i];
      if (Object.prototype.hasOwnProperty.call(data, k)) {
        if (lsGet(k) !== data[k]) { lsSet(k, data[k]); changed = true; }
      }
    }
    return changed;
  }

  // ── server sync ───────────────────────────────────────────────────────────
  function localMtime() { return parseInt(lsGet(MTIME_KEY) || "0", 10) || 0; }

  function pushNow() {
    if (!state.user) return;
    var snap = snapshot();
    lastSnapshot = JSON.stringify(snap);
    api("/api/data", { method: "PUT", body: { data: snap } }).then(function (r) {
      if (r.ok && r.body && r.body.updatedAt) lsSet(MTIME_KEY, String(r.body.updatedAt));
    });
  }
  function schedulePush() {
    if (!state.user) return;
    if (pushTimer) clearTimeout(pushTimer);
    pushTimer = setTimeout(pushNow, 1200);
  }

  // Called on load/login. Reconciles server <-> local by last-write-wins.
  function reconcile() {
    if (!state.user) return Promise.resolve();
    return api("/api/data", { method: "GET" }).then(function (r) {
      var serverData = r.ok && r.body ? r.body.data : null;
      var serverMtime = r.ok && r.body ? (r.body.updatedAt || 0) : 0;
      lastSnapshot = JSON.stringify(snapshot());

      var serverNewer = serverData && serverMtime >= localMtime();
      if (serverNewer) {
        var changed = applyBlob(serverData);
        lsSet(MTIME_KEY, String(serverMtime));
        lastSnapshot = JSON.stringify(snapshot());
        // If the server's copy differs from what's on screen, reload once so the
        // React app re-reads localStorage. Guarded so we never loop.
        if (changed && !sessionStorage.getItem(RELOAD_FLAG)) {
          try { sessionStorage.setItem(RELOAD_FLAG, "1"); } catch (e) {}
          location.reload();
        }
      } else {
        // Local is newer (or the account has no saved data yet): push it up.
        pushNow();
      }
    });
  }

  function startSyncLoop() {
    setInterval(function () {
      if (!state.user) return;
      var snap = JSON.stringify(snapshot());
      if (snap !== lastSnapshot) { lastSnapshot = snap; schedulePush(); }
    }, 4000);
    window.addEventListener("pagehide", function () {
      if (!state.user) return;
      var snap = JSON.stringify(snapshot());
      if (snap === lastSnapshot) return;
      try {
        fetch("/api/data", {
          method: "PUT", credentials: "same-origin", keepalive: true,
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ data: snapshot() }),
        });
      } catch (e) {}
    });
  }

  // ── UI ────────────────────────────────────────────────────────────────────
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === "text") n.textContent = attrs[k];
      else if (k === "html") n.innerHTML = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }
    if (kids) for (var i = 0; i < kids.length; i++) if (kids[i]) n.appendChild(kids[i]);
    return n;
  }

  function injectStyles() {
    var css =
      // Header-integrated: matches the site's 32px icon buttons and themes via the
      // app's CSS vars. .afw-fixed is the fallback if the header isn't found.
      "#afw-acct-btn{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 12px;" +
      "border-radius:7px;border:1px solid var(--b2,rgba(128,128,128,.35));background:var(--s2,#161a22);" +
      "color:var(--tx,#e8ebf0);font-family:inherit;font-size:13px;font-weight:600;line-height:1;" +
      "cursor:pointer;flex-shrink:0;margin-right:8px;white-space:nowrap;box-sizing:border-box}" +
      "#afw-acct-btn:hover{filter:brightness(1.08)}" +
      "#afw-acct-btn.afw-in{width:32px;padding:0;justify-content:center}" +
      "#afw-acct-btn.afw-fixed{position:fixed;top:calc(env(safe-area-inset-top,0px) + 10px);right:10px;" +
      "z-index:2147483000;box-shadow:0 4px 14px rgba(0,0,0,.28)}" +
      "#afw-acct-btn .afw-dot{width:22px;height:22px;border-radius:50%;display:flex;" +
      "align-items:center;justify-content:center;background:#3b6cf5;color:#fff;font-weight:700;font-size:11px}" +
      "#afw-overlay{position:fixed;inset:0;z-index:2147483001;background:rgba(6,8,12,.62);" +
      "display:flex;align-items:center;justify-content:center;padding:16px;" +
      "backdrop-filter:blur(3px);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}" +
      "#afw-card{width:100%;max-width:360px;background:#12151c;color:#e8ebf0;border-radius:16px;" +
      "border:1px solid rgba(128,128,128,.22);box-shadow:0 20px 60px rgba(0,0,0,.5);" +
      "padding:22px 22px 20px;position:relative}" +
      "@media (prefers-color-scheme:light){#afw-card{background:#fff;color:#12151c}}" +
      "#afw-card h2{margin:0 0 4px;font-size:19px;font-weight:800;letter-spacing:-.01em}" +
      "#afw-card .afw-sub{margin:0 0 18px;font-size:13px;opacity:.62}" +
      "#afw-card label{display:block;font-size:12px;font-weight:600;opacity:.8;margin:12px 0 5px}" +
      "#afw-card input{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:10px;" +
      "border:1px solid rgba(128,128,128,.32);background:rgba(128,128,128,.08);color:inherit;" +
      "font-size:14px;outline:none}" +
      "#afw-card input:focus{border-color:#3b6cf5}" +
      "#afw-card .afw-primary{width:100%;margin-top:18px;padding:11px;border:0;border-radius:10px;" +
      "background:#3b6cf5;color:#fff;font-size:14px;font-weight:700;cursor:pointer}" +
      "#afw-card .afw-primary:disabled{opacity:.55;cursor:default}" +
      "#afw-card .afw-link{background:none;border:0;color:#7aa2ff;font-size:13px;cursor:pointer;padding:0}" +
      "#afw-card .afw-foot{margin-top:16px;text-align:center;font-size:13px;opacity:.8}" +
      "#afw-card .afw-err{margin-top:12px;color:#ff6b6b;font-size:13px;min-height:0}" +
      "#afw-card .afw-x{position:absolute;top:12px;right:14px;background:none;border:0;" +
      "color:inherit;opacity:.5;font-size:22px;line-height:1;cursor:pointer}" +
      "#afw-card .afw-or{display:flex;align-items:center;gap:10px;margin:18px 0 4px;opacity:.5;font-size:12px}" +
      "#afw-card .afw-or:before,#afw-card .afw-or:after{content:'';flex:1;height:1px;background:currentColor;opacity:.3}" +
      "#afw-gbtn{display:flex;justify-content:center;margin-top:14px;min-height:0}" +
      "#afw-card .afw-you{font-size:13px;opacity:.7;margin:0 0 16px;word-break:break-all}";
    document.head.appendChild(el("style", { text: css }));
  }

  var overlay = null;
  function closeModal() { if (overlay) { overlay.remove(); overlay = null; } }

  function openModal(mode) {
    closeModal();
    if (state.user) return openAccount();
    mode = mode || "login";
    var isLogin = mode === "login";

    var err = el("div", { class: "afw-err" });
    var email = el("input", { type: "email", autocomplete: "email", placeholder: "you@email.com" });
    var pass = el("input", { type: "password", autocomplete: isLogin ? "current-password" : "new-password", placeholder: "••••••••" });
    var submit = el("button", { class: "afw-primary", text: isLogin ? "Sign in" : "Create account" });
    var gbtn = el("div", { id: "afw-gbtn" });

    function doSubmit() {
      err.textContent = "";
      var e = email.value.trim(), p = pass.value;
      if (!e || !p) { err.textContent = "Enter your email and password."; return; }
      if (!isLogin && p.length < 8) { err.textContent = "Password must be at least 8 characters."; return; }
      submit.disabled = true;
      submit.textContent = isLogin ? "Signing in…" : "Creating…";
      api("/api/auth/" + (isLogin ? "login" : "register"), { method: "POST", body: { email: e, password: p } })
        .then(function (r) {
          if (!r.ok) {
            err.textContent = (r.body && r.body.error) || "Something went wrong.";
            submit.disabled = false; submit.textContent = isLogin ? "Sign in" : "Create account";
            return;
          }
          onAuthed(r.body.user);
        });
    }
    submit.addEventListener("click", doSubmit);
    pass.addEventListener("keydown", function (ev) { if (ev.key === "Enter") doSubmit(); });

    var toggle = el("button", { class: "afw-link", text: isLogin ? "Create one" : "Sign in" });
    toggle.addEventListener("click", function () { openModal(isLogin ? "register" : "login"); });

    var card = el("div", { id: "afw-card" }, [
      el("button", { class: "afw-x", text: "×" }),
      el("h2", { text: isLogin ? "Welcome back" : "Create your account" }),
      el("p", { class: "afw-sub", text: "Save your team and watchlist across devices." }),
      el("label", { text: "Email" }), email,
      el("label", { text: "Password" }), pass,
      submit, err,
      el("div", { class: "afw-or", text: "or" }),
      gbtn,
      el("div", { class: "afw-foot" }, [
        document.createTextNode(isLogin ? "New here? " : "Have an account? "), toggle,
      ]),
    ]);
    card.querySelector(".afw-x").addEventListener("click", closeModal);

    overlay = el("div", { id: "afw-overlay" }, [card]);
    overlay.addEventListener("click", function (ev) { if (ev.target === overlay) closeModal(); });
    document.body.appendChild(overlay);
    setTimeout(function () { email.focus(); }, 30);
    renderGoogle(gbtn);
  }

  function openAccount() {
    closeModal();
    var u = state.user;
    var signout = el("button", { class: "afw-primary", text: "Sign out" });
    signout.addEventListener("click", function () {
      signout.disabled = true; signout.textContent = "Signing out…";
      api("/api/auth/logout", { method: "POST" }).then(function () {
        state.user = null;
        try { sessionStorage.removeItem(RELOAD_FLAG); } catch (e) {}
        renderButton();
        closeModal();
      });
    });
    var card = el("div", { id: "afw-card" }, [
      el("button", { class: "afw-x", text: "×" }),
      el("h2", { text: "Your account" }),
      el("p", { class: "afw-you", text: u.email + (u.google ? "  ·  Google" : "") }),
      el("p", { class: "afw-sub", text: "Your team, watchlist and preferences sync automatically." }),
      signout,
    ]);
    card.querySelector(".afw-x").addEventListener("click", closeModal);
    overlay = el("div", { id: "afw-overlay" }, [card]);
    overlay.addEventListener("click", function (ev) { if (ev.target === overlay) closeModal(); });
    document.body.appendChild(overlay);
  }

  // ── Google Sign-In (ID token flow, verified server-side) ──
  var gisLoading = null;
  function loadGis() {
    if (window.google && window.google.accounts && window.google.accounts.id) return Promise.resolve();
    if (gisLoading) return gisLoading;
    gisLoading = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = "https://accounts.google.com/gsi/client";
      s.async = true; s.defer = true;
      s.onload = resolve; s.onerror = reject;
      document.head.appendChild(s);
    });
    return gisLoading;
  }
  function renderGoogle(container) {
    if (!state.googleClientId) { container.style.display = "none"; return; }
    loadGis().then(function () {
      window.google.accounts.id.initialize({
        client_id: state.googleClientId,
        callback: function (resp) {
          api("/api/auth/google", { method: "POST", body: { credential: resp.credential } })
            .then(function (r) { if (r.ok) onAuthed(r.body.user); });
        },
      });
      window.google.accounts.id.renderButton(container, {
        theme: "outline", size: "large", shape: "pill", text: "continue_with", width: 316,
      });
    }).catch(function () { container.style.display = "none"; });
  }

  function onAuthed(user) {
    state.user = user;
    try { sessionStorage.removeItem(RELOAD_FLAG); } catch (e) {}
    renderButton();
    closeModal();
    reconcile();
  }

  // ── account button, injected into the site header's top-right control cluster
  //    (just left of the menu / theme icons). A MutationObserver re-places it so
  //    it survives React re-renders; if the header can't be found it falls back
  //    to a fixed top-right button. ──
  var acctBtn = null, _moTimer = null;

  function paintButton() {
    if (!acctBtn) return;
    var key = state.user ? "in:" + (state.user.email || "") : "out";
    if (acctBtn.getAttribute("data-st") === key) return; // avoid needless repaint
    acctBtn.setAttribute("data-st", key);
    acctBtn.innerHTML = "";
    if (state.user) {
      acctBtn.classList.add("afw-in");
      acctBtn.title = "Account";
      acctBtn.appendChild(el("span", { class: "afw-dot", text: (state.user.email || "?").charAt(0).toUpperCase() }));
    } else {
      acctBtn.classList.remove("afw-in");
      acctBtn.title = "Sign in";
      acctBtn.appendChild(el("span", { text: "Sign in" }));
    }
  }

  function renderButton() {
    if (!acctBtn) {
      acctBtn = el("button", { id: "afw-acct-btn", type: "button" });
      acctBtn.addEventListener("click", function () { state.user ? openAccount() : openModal("login"); });
    }
    paintButton();
    var menu = document.querySelector(".mobile-menu-btn"); // last icon in the header cluster
    if (menu && menu.parentNode) {
      acctBtn.classList.remove("afw-fixed");
      if (acctBtn.parentNode !== menu.parentNode || acctBtn.nextSibling !== menu) {
        menu.parentNode.insertBefore(acctBtn, menu);
      }
    } else if (!acctBtn.parentNode) {
      acctBtn.classList.add("afw-fixed");
      document.body.appendChild(acctBtn);
    }
  }

  // Re-place the button whenever the app re-renders the header (debounced).
  function watchHeader() {
    var mo = new MutationObserver(function () {
      if (_moTimer) return;
      _moTimer = setTimeout(function () { _moTimer = null; renderButton(); }, 200);
    });
    mo.observe(document.body, { childList: true, subtree: true });
  }

  // ── boot ──
  function boot() {
    injectStyles();
    renderButton();
    watchHeader();
    startSyncLoop();
    api("/api/auth/config", { method: "GET" }).then(function (r) {
      if (r.ok && r.body) state.googleClientId = r.body.googleClientId || "";
    });
    api("/api/auth/me", { method: "GET" }).then(function (r) {
      if (r.ok && r.body && r.body.user) {
        state.user = r.body.user;
        renderButton();
        reconcile();
      }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
