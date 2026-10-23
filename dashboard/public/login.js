document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  try {
    const r = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: document.getElementById("pw").value }),
    });
    if (r.ok) return location.replace("/");
    err.textContent = (await r.json().catch(() => ({}))).error || "Sign-in failed.";
  } catch {
    err.textContent = "Network error.";
  }
});
