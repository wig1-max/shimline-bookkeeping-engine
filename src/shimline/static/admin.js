"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const button = document.getElementById("mobileMenu");
  const sidebar = document.getElementById("sidebar");
  if (!button || !sidebar) return;
  button.addEventListener("click", () => {
    const open = sidebar.classList.toggle("open");
    button.setAttribute("aria-expanded", String(open));
  });
});
