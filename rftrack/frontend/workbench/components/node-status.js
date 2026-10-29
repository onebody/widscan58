/**
 * 节点状态：**只读后端真有的东西**（`GET /api/nodes` 的推导结果）。这里没有任何演示数据：
 * 读不到就是空状态，能力没声明就是 `unsupported`——把"这块板大概能做"补出来是规范 §2.3
 * 明令禁止的事。
 *
 * 每块板显示的行都由服务端字段直接决定：`transport.kind` 决定"接入它会不会停掉这台
 * 接入机的 RotorHazard 计时"（`stops_rh` 是介质属性，不是数据行说了算，R21），
 * 能力项则来自 `capabilities` 的声明渲染。
 */

import { fmtClock, empty, sourceBadge } from "./parts.js";

export const plugin_id = "node-status";

const CAPABILITY_TEXT = {
  levels: "电平帧",
  spectrum: "频谱",
  rawiq: "RAW IQ",
};

const STATUS_TEXT = { active: "在活动", lost: "已失联", disabled: "已停用" };

export function create(rt, deps) {
  const { h, ref } = rt;

  return {
    name: "NodeStatus",
    props: { widget: { type: Object, required: true } },
    setup(props) {
      const widget = props.widget;
      const beat = ref(0);
      // 这块板表是"现在连着谁"，跟着接入机的心跳节奏刷；比演示组件慢一档，2 s 起。
      function schedule() {
        widget.lease.setTimeout(() => {
          beat.value += 1;
          schedule();
        }, 2000);
      }
      schedule();

      const capabilityCell = (name, declared) => {
        const reason = declared && declared.reason;
        if (declared && declared.supported === null) {
          // 不认识 ≠ 不支持：说成不支持就等于把板端报过的一句话改写成"没说过"。
          return h(
            "span",
            { class: "cap cap--unknown", title: `板端原样声明：${JSON.stringify(declared.declared)}` },
            REASON_TEXT.unknown_capability
          );
        }
        const label = declared && declared.supported
          ? `支持${limitsText(declared) ? ` · ${limitsText(declared)}` : ""}`
          : `不支持（${REASON_TEXT[reason] || reason || "未声明"}）`;
        return h("span", { class: declared && declared.supported ? "cap cap--on" : "cap cap--off" }, label);
      };

      return () => {
        beat.value;
        deps.signal.tick.value;
        const boards = deps.context.state.scopeNodes;
        const gateway = deps.context.gateway();
        if (!boards.length) {
          return empty(
            rt,
            gateway ? `接入机「${gateway.name}」下没有节点` : "当前位置里没有选中的接入机",
            gateway && gateway.dial_state !== "online" ? `拨号态：${gateway.dial_state || "未拨号"}` : "板子插好后由后台拨号循环登记"
          );
        }
        return h("div", { class: "nodes" }, [
          h("p", { class: "meta" }, [
            sourceBadge(rt, "api"),
            ` ${boards.length} 块板 · 接入机 ${gateway ? gateway.name : "未选"}${gateway ? `（${gateway.dial_state || "未拨号"}）` : ""}`,
          ]),
          h(
            "ul",
            { class: "node-list" },
            boards.map((node) => {
              const selected = deps.context.state.nodeId === node.node_id;
              return h("li", {
                class: ["node", selected ? "is-selected" : null, `node--${node.status}`],
                onClick: () => {
                  deps.actions.selectNode(node.node_id);
                },
              }, [
                h("div", { class: "node-title" }, [
                  h("strong", node.display_name),
                  h("span", { class: `chip chip--${node.status}` }, STATUS_TEXT[node.status] || node.status),
                  node.stops_rh ? h("span", { class: "chip chip--warn", title: "接入这块板会停掉这台接入机的 RotorHazard 计时" }, "会停 RH") : null,
                ]),
                h("div", { class: "node-line muted" }, [
                  `传输 ${node.transport.kind || "未知"}${node.transport.addr ? ` · ${node.transport.addr}` : ""}`,
                  node.fw_version ? ` · 固件 ${node.fw_version}` : " · 固件未上报",
                  node.protocol_version === null || node.protocol_version === undefined
                    ? " · 协议未上报"
                    : ` · 协议 v${node.protocol_version}`,
                  ` · 最近 ${fmtClock(node.last_seen, "UTC")}`,
                ]),
                node.declaration_error
                  ? h("div", { class: "warn" }, `能力声明有问题（${node.declaration_error}）：全部按不支持渲染`)
                  : null,
                h(
                  "div",
                  { class: "caps" },
                  Object.keys(node.capabilities || {}).map((name) =>
                    h("span", { class: "cap-row" }, [`${CAPABILITY_TEXT[name] || name}：`, capabilityCell(name, node.capabilities[name])])
                  )
                ),
              ]);
            })
          ),
        ]);
      };
    },
  };
}

const REASON_TEXT = {
  undeclared: "板端未声明",
  declared_unsupported: "板端声明不支持",
  missing_limits: "声明缺上限",
  malformed: "声明格式非法",
  malformed_json: "声明格式非法",
  not_an_object: "声明不是一个对象",
  // `supported === null` 是"板端说了，但本系统不认识这一项"（services/capabilities.py 最后那段）。
  // 把它并进"不支持"是反的：那不诚实，还会让人以为板子没报——原样呈现才是这里的纪律。
  unknown_capability: "本系统不认识这一项",
};

/**
 * 有界能力必须把上限说出来：只说"支持频谱"而不说 512 bins，等于把有界渲染成无界。
 *
 * 上限是**平铺在能力对象上**的（`{"supported": true, "max_bins": 512}`，见
 * `services/capabilities.py` 的 `rendered[name] = {"supported": True, **limits}`），
 * 没有 `limits` 这个子对象。浏览器那一轮就是靠这条抓到组件读错了形状。
 */
const LIMIT_KEYS = [
  ["max_bins", (value) => `${value} bins`],
  ["paced_rotation_hz", (value) => `${value} Hz 转速`],
];

function limitsText(declared) {
  return LIMIT_KEYS
    .filter(([key]) => typeof declared[key] === "number" && declared[key])
    .map(([key, show]) => show(declared[key]))
    .join(" · ");
}
