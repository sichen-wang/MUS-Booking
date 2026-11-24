// MUS Booking：页面上仅有的几处脚本。导航和表单由 htmx 的 hx-boost 处理。
(() => {
  "use strict";

  const minutes = (text) => {
    const [h, m] = text.split(":").map(Number);
    return h * 60 + m;
  };
  const hhmm = (value) => `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;

  // 选择框是透明地盖在按钮上的原生控件；选中之后把文字同步到按钮上
  function show(select) {
    const picker = select.closest(".picker");
    if (!picker) return;
    if (select.name === "room") {
      const option = select.selectedOptions[0];
      picker.querySelector("[data-show=room-code]").textContent = option ? option.dataset.code : "";
      picker.querySelector("[data-show=room-piano]").textContent = option ? option.dataset.piano : "";
    } else {
      picker.querySelector(".picker-value").textContent = select.value;
    }
  }

  function choose(select, value) {
    const values = [...select.options].map((o) => o.value);
    if (!values.length) return;
    const target = values.includes(value) ? value : values[values.length - 1];
    select.value = target;
    show(select);
  }

  function mark(form) {
    if (!form || !form.start) return;
    const length = minutes(form.end.value) - minutes(form.start.value);
    form.querySelectorAll("[data-hours]").forEach((chip) => {
      chip.classList.toggle("on", length === Number(chip.dataset.hours) * 60);
    });
    form.querySelectorAll("[data-room]").forEach((chip) => {
      chip.classList.toggle("on", chip.dataset.room === form.room.value);
    });
    form.dataset.length = String(length > 0 ? length : 120);
  }

  function setUp(root) {
    root.querySelectorAll(".picker select").forEach(show);
    root.querySelectorAll("form.booking").forEach(mark);
  }

  document.addEventListener("change", (event) => {
    const select = event.target.closest(".picker select");
    if (!select) return;
    const form = select.form;
    if (select.name === "start" && form.end) {
      // 改开始时间时保持原来的时长
      choose(form.end, hhmm(minutes(select.value) + Number(form.dataset.length || 120)));
    }
    show(select);
    mark(form);
  });

  function arm(button) {
    const label = button.querySelector("span") || button;
    const original = label.textContent;
    label.textContent = button.dataset.confirm;
    button.classList.add("armed");
    setTimeout(() => {
      if (!button.isConnected) return;
      label.textContent = original;
      button.classList.remove("armed");
    }, 3000);
  }

  document.addEventListener("click", (event) => {
    const hours = event.target.closest("[data-hours]");
    if (hours) {
      const form = hours.form;
      choose(form.end, hhmm(minutes(form.start.value) + Number(hours.dataset.hours) * 60));
      mark(form);
      return;
    }
    const room = event.target.closest("[data-room]");
    if (room) {
      const form = room.form;
      form.room.value = room.dataset.room;
      show(form.room);
      mark(form);
      return;
    }
    const confirm = event.target.closest("[data-confirm]");
    if (confirm && !confirm.classList.contains("armed")) {
      event.preventDefault();
      arm(confirm);
      return;
    }
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const label = copy.querySelector("span") || copy;
      const original = label.textContent;
      const done = () => {
        label.textContent = "已复制";
        setTimeout(() => { label.textContent = original; }, 1500);
      };
      if (navigator.clipboard) navigator.clipboard.writeText(copy.dataset.copy).then(done, () => {});
    }
  });

  setUp(document);
  document.addEventListener("htmx:afterSettle", (event) => setUp(event.detail.elt || document));
})();
