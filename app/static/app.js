(function () {
  // Auto-refresh (the checkbox is missing on the login and setup pages)
  var box = document.getElementById("autorefresh");
  var key = "smtp-relay-autorefresh";
  var timer = null;
  function schedule() {
    clearTimeout(timer);
    if (box && box.checked) timer = setTimeout(function () { location.reload(); }, 5000);
  }
  if (box) {
    try { box.checked = localStorage.getItem(key) === "1"; } catch (e) {}
    box.addEventListener("change", function () {
      try { localStorage.setItem(key, box.checked ? "1" : "0"); } catch (e) {}
      schedule();
    });
    // Don't reload while the user is typing in a form.
    document.addEventListener("input", function () { clearTimeout(timer); });
    schedule();
  }
  // Column names as labels for the card layout of lists on small screens
  document.querySelectorAll("table.list").forEach(function (table) {
    var heads = Array.prototype.map.call(table.querySelectorAll("thead th"), function (th) {
      return th.textContent.trim();
    });
    table.querySelectorAll("tbody tr").forEach(function (tr) {
      Array.prototype.forEach.call(tr.children, function (td, i) {
        if (heads[i] && td.colSpan === 1) td.dataset.label = heads[i];
        if (!td.textContent.trim() && !td.querySelector("form, button")) td.classList.add("empty");
      });
    });
  });
  document.querySelectorAll("tr.clickable").forEach(function (tr) {
    tr.addEventListener("click", function (ev) {
      if (ev.target.closest("a") || window.getSelection().toString()) return;
      location.href = tr.dataset.href;
    });
  });
})();
