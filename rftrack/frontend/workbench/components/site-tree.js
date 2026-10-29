/**
 * 场地层级树：场地 → 位置 → 接入机 → 节点，点一层就换视图。
 *
 * "换视图不停后台"在这里是可验证的：这个组件只调 `deps.actions.*`（由 `app.js` 提供，
 * 内部只碰 `context` 的选中态与 `layouts` 的读取），一次也不会打到写端点。
 * `api.audit()` 就是拿来做这条断言的。
 */

import { empty } from "./parts.js";

export const plugin_id = "site-tree";

export function create(rt, deps) {
  const { h } = rt;

  return {
    name: "SiteTree",
    props: { widget: { type: Object, required: true } },
    setup(props) {
      return () => {
        deps.signal.tick.value;
        const ctx = deps.context.state;
        if (!ctx.sites.length) {
          return empty(rt, "库里还没有场地", "S2 的 sites 接口通、但没有登记过任何场地");
        }
        return h("div", { class: "tree" }, [
          h("p", { class: "meta" }, "点一层就切视图；后台拨号与采集不受影响"),
          h(
            "ul",
            { class: "tree-list" },
            ctx.sites.map((site) => {
              const current = site.site_id === ctx.siteId;
              const lag = current ? deps.context.lag(site) : null;
              return h("li", { class: ["site", current ? "is-current" : null] }, [
                h("button", {
                  class: "tree-node tree-node--site",
                  type: "button",
                  onClick: () => deps.actions.selectSite(site.site_id),
                }, [
                  h("span", { class: "tree-name" }, site.name),
                  h("span", { class: `chip chip--${site.status}` }, site.status === "active" ? "在用" : site.status),
                  site.origin === "aggregated" ? h("span", { class: "chip chip--agg" }, "汇聚") : null,
                ]),
                current && lag
                  ? h("p", { class: ["lag", lag.stale ? "lag--stale" : null] }, `聚合视图：${lag.label}`)
                  : null,
                current
                  ? h(
                      "ul",
                      { class: "tree-sub" },
                      ctx.relays.map((relay) => {
                        const relayOn = relay.relay_id === ctx.relayId;
                        const gateways = relayOn || ctx.relayId === null
                          ? ctx.scopeGateways.filter((row) => row.relay_id === relay.relay_id)
                          : ctx.gateways.filter((row) => row.relay_id === relay.relay_id);
                        return h("li", { class: ["relay", relayOn ? "is-current" : null] }, [
                          h("button", {
                            class: "tree-node tree-node--relay",
                            type: "button",
                            onClick: () => deps.actions.selectRelay(relay.relay_id),
                          }, [
                            h("span", { class: "tree-name" }, relay.name),
                            h("span", { class: "muted" }, `${gateways.length} 台接入机`),
                          ]),
                          h(
                            "ul",
                            { class: "tree-sub" },
                            gateways.map((gateway) => {
                              const on = gateway.gateway_id === ctx.gatewayId;
                              const boards = ctx.nodes.filter((row) => row.gateway_id === gateway.gateway_id);
                              return h("li", { class: ["gateway", on ? "is-current" : null] }, [
                                h("button", {
                                  class: "tree-node tree-node--gateway",
                                  type: "button",
                                  onClick: () => deps.actions.selectGateway(gateway.gateway_id),
                                }, [
                                  h("span", { class: "tree-name" }, gateway.name),
                                  h("span", { class: `chip chip--${gateway.dial_state || "idle"}` }, DIAL_TEXT[gateway.dial_state] || "未拨号"),
                                  h("span", { class: "muted" }, `${boards.length} 块板`),
                                ]),
                                on
                                  ? h(
                                      "ul",
                                      { class: "tree-sub" },
                                      boards.length
                                        ? boards.map((node) =>
                                            h("li", [
                                              h("button", {
                                                class: ["tree-node", "tree-node--node", ctx.nodeId === node.node_id ? "is-current" : null],
                                                type: "button",
                                                onClick: () => deps.actions.selectNode(node.node_id),
                                              }, [
                                                h("span", { class: "tree-name" }, node.display_name),
                                                h("span", { class: `chip chip--${node.status}` }, node.status),
                                              ]),
                                            ])
                                          )
                                        : [h("li", { class: "muted" }, "这块接入机下没有节点")]
                                    )
                                  : null,
                              ]);
                            })
                          ),
                        ]);
                      })
                    )
                  : null,
              ]);
            })
          ),
        ]);
      };
    },
  };
}

const DIAL_TEXT = { online: "在线", dialing: "拨号中", unreachable: "不通" };
