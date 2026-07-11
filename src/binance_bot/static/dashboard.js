const refreshSeconds = Number(document.body.dataset.refreshSeconds || "15");
const countdownElement = document.getElementById("refresh-countdown");

let remaining = refreshSeconds;
if (countdownElement) {
  countdownElement.textContent = String(remaining);
  window.setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      remaining = refreshSeconds;
    }
    countdownElement.textContent = String(remaining);
  }, 1000);
}

for (const form of document.querySelectorAll(".action-form[data-confirm]")) {
  form.addEventListener("submit", (event) => {
    const message = form.getAttribute("data-confirm") || "Confirm action?";
    if (!window.confirm(message)) {
      event.preventDefault();
      return;
    }

    const button = form.querySelector("button");
    if (button instanceof HTMLButtonElement) {
      button.disabled = true;
      button.textContent = "Submitting...";
    }
  });
}