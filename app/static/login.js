"use strict";

document.getElementById("loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.getElementById("loginBtn");
  const error = document.getElementById("loginError");
  error.hidden = true;
  button.disabled = true;
  try {
    const body = new FormData();
    body.append("password", document.getElementById("password").value);
    const res = await fetch("/api/login", { method: "POST", body });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Accesso non riuscito.");
    window.location.href = "/";
  } catch (err) {
    error.textContent = err.message === "Failed to fetch" ? "Il server non risponde." : err.message;
    error.hidden = false;
    document.getElementById("password").select();
  } finally {
    button.disabled = false;
  }
});
