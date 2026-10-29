/**
 * 注册表里查无此件的格子。
 *
 * 规范 §3.3 那条"不能崩"落在这里：**保留它的位置与配置，把"我不认识它"说出来**，
 * 而不是抹掉一块格子或整屏白掉——组件可能来自另一台版本更新的实例，也可能只是这次
 * 前端少装了一个文件，两种情况都不该让运营者以为数据没了。
 */

export const plugin_id = "*unknown*";

export function create(rt, deps) {
  const { h } = rt;

  return {
    name: "UnknownPlugin",
    props: { widget: { type: Object, required: true } },
    setup(props) {
      return () => {
        const widget = props.widget;
        deps.signal.tick.value;
        return h("div", { class: "unknown" }, [
          h("p", [
            h("strong", "组件未注册："),
            h("code", widget.plugin_id),
          ]),
          h("p", { class: "muted" }, `位置 ${widget.x},${widget.y} · ${widget.w}×${widget.h} 与 ${Object.keys(widget.config).length} 项配置都保留着；装上对应组件后会自动回来。`),
          widget.declaration_error
            ? h("p", { class: "warn" }, `配置行本身也报问题：${widget.declaration_error}`)
            : null,
        ]);
      };
    },
  };
}
