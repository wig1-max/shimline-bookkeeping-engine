"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const canvas = document.getElementById("serviceRiskChart");
  if (!canvas || typeof Chart === "undefined") return;
  const values = (canvas.dataset.values || "").split(",").map(Number);
  if (values.length !== 4 || values.some((value) => !Number.isFinite(value))) return;
  new Chart(canvas, {
    type: "bar",
    data: {
      labels: ["Overdue", "Due soon", "Waiting", "Review"],
      datasets: [{
        data: values,
        backgroundColor: ["#ef6f5e", "#e2b44f", "#65a6a0", "#8d82c4"],
        borderWidth: 0,
        borderRadius: 5
      }]
    },
    options: {
      animation: false,
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: {
        x: { grid: { display: false } },
        y: { beginAtZero: true, ticks: { precision: 0 } }
      }
    }
  });
});
