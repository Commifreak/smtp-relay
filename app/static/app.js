(function () {
  var box = document.getElementById("autorefresh");
  var key = "mta-relay-autorefresh";
  var on = false;
  try { on = localStorage.getItem(key) === "1"; } catch (e) {}
  box.checked = on;
  var timer = null;
  function schedule() {
    clearTimeout(timer);
    if (box.checked) timer = setTimeout(function () { location.reload(); }, 5000);
  }
  box.addEventListener("change", function () {
    try { localStorage.setItem(key, box.checked ? "1" : "0"); } catch (e) {}
    schedule();
  });
  // Don't reload while the user is typing in a form.
  document.addEventListener("input", function () { clearTimeout(timer); });
  schedule();
  document.querySelectorAll("tr.clickable").forEach(function (tr) {
    tr.addEventListener("click", function (ev) {
      if (ev.target.closest("a") || window.getSelection().toString()) return;
      location.href = tr.dataset.href;
    });
  });
})();
