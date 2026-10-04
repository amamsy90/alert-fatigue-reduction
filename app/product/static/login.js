// One sign-in for the pages. A 401 asks for the password once and stores the
// session cookie; later requests do not send the password again.
(function () {
  const orig = window.fetch.bind(window);
  let pending = null;

  function ask() {
    if (pending) return pending;
    pending = new Promise((resolve) => {
      const panel = document.createElement("form");
      panel.setAttribute("style",
        "position:fixed;inset:0;background:rgba(28,58,92,.45);display:flex;align-items:center;justify-content:center;z-index:20");
      panel.innerHTML =
        '<div style="background:#fff;padding:1.25rem;border-radius:6px;min-width:16rem">' +
        '<p style="margin:0 0 .75rem;font-family:Arial,sans-serif;color:#1c3a5c">Sign in</p>' +
        '<input name="username" autocomplete="username" placeholder="user" style="display:block;width:100%;margin-bottom:.5rem;padding:.4rem">' +
        '<input name="password" type="password" autocomplete="current-password" placeholder="password" style="display:block;width:100%;margin-bottom:.75rem;padding:.4rem">' +
        '<button type="submit" style="background:#1c3a5c;color:#fff;border:0;padding:.45rem .8rem">Sign in</button>' +
        '<p class="err" style="color:#8b3a3a;min-height:1.2rem;font-size:.85rem"></p></div>';
      document.body.appendChild(panel);
      panel.addEventListener("submit", async (event) => {
        event.preventDefault();
        const data = new FormData(panel);
        const res = await orig("/login", {
          method: "POST",
          headers: { "content-type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ username: data.get("username"), password: data.get("password") }),
        });
        if (res.ok) {
          panel.remove();
          pending = null;
          resolve();
          return;
        }
        panel.querySelector(".err").textContent = res.status === 429
          ? "Too many attempts. Wait and try again."
          : "That sign-in was refused.";
      });
    });
    return pending;
  }

  window.fetch = async function (url, opts) {
    const res = await orig(url, opts);
    if (res.status !== 401 || String(url).indexOf("/login") !== -1) return res;
    await ask();
    return orig(url, opts);
  };
})();
