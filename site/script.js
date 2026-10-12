(function () {
  // 滚动进入动画
  var items = document.querySelectorAll('.reveal');
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce || !('IntersectionObserver' in window)) {
    items.forEach(function (el) { el.classList.add('in'); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
      });
    }, { threshold: 0.12 });
    items.forEach(function (el) { io.observe(el); });
  }

  // 遥控器示意图：悬停/聚焦显示按键说明
  var tip = document.getElementById('remoteTip');
  var remote = document.getElementById('remote');
  if (tip && remote) {
    var defTitle = tip.querySelector('strong').textContent;
    var defDesc = tip.querySelector('span').textContent;
    var show = function (btn) {
      tip.querySelector('strong').textContent = btn.dataset.name;
      tip.querySelector('span').textContent = btn.dataset.desc;
      tip.classList.toggle('voice', !!btn.dataset.voice);
    };
    var reset = function () {
      tip.querySelector('strong').textContent = defTitle;
      tip.querySelector('span').textContent = defDesc;
      tip.classList.remove('voice');
    };
    remote.querySelectorAll('.hs').forEach(function (b) {
      b.addEventListener('mouseenter', function () { show(b); });
      b.addEventListener('focus', function () { show(b); });
      b.addEventListener('click', function () { show(b); });
      b.addEventListener('mouseleave', reset);
      b.addEventListener('blur', reset);
    });
  }
})();
