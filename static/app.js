// Small progressive enhancements. Kept in a file (not inline) so the
// Content-Security-Policy can forbid inline scripts entirely. Every page
// still works with JavaScript turned off.

// Ask before destructive actions (forms carrying data-confirm).
document.addEventListener("submit", function (event) {
  var message = event.target.getAttribute("data-confirm");
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});

// Filter dropdowns apply as soon as an option is picked; the Search/Filter
// button still works, and is what applies them when JavaScript is off.
document.addEventListener("change", function (event) {
  var select = event.target.closest("select[data-autosubmit]");
  if (!select || !select.form) return;
  if (select.form.requestSubmit) {
    select.form.requestSubmit();
  } else {
    select.form.submit();
  }
});

function closeToast(toast) {
  if (!toast || toast.classList.contains("leaving")) return;
  toast.classList.add("leaving");
  setTimeout(function () { toast.remove(); }, 260);
}

document.addEventListener("click", function (event) {
  // Print button on the e-ticket.
  if (event.target.closest("[data-print]")) {
    window.print();
  }
  // Dismiss a notification.
  var dismiss = event.target.closest("[data-dismiss]");
  if (dismiss) {
    closeToast(dismiss.closest(".toast"));
  }
  // Close the account menu when clicking anywhere else.
  document.querySelectorAll("details.user-menu[open]").forEach(function (menu) {
    if (!menu.contains(event.target)) menu.open = false;
  });
});

// Success messages fade away on their own; errors stay until dismissed.
document.querySelectorAll(".toast[data-autohide]").forEach(function (toast) {
  setTimeout(function () { closeToast(toast); }, 6000);
});

// Escape closes the account menu and the mobile navigation.
document.addEventListener("keydown", function (event) {
  if (event.key !== "Escape") return;
  document.querySelectorAll("details.user-menu[open]").forEach(function (menu) {
    menu.open = false;
    menu.querySelector("summary").focus();
  });
  var navCheck = document.getElementById("nav-check");
  if (navCheck) navCheck.checked = false;
});
