/**
 * 场地层级树：场地 → 位置 → 接入机 → 节点，点一层就换视图。
 *
 * "换视图不停后台"在这里是可验证的：这个组件只调 `deps.actions.*`（由 `app.js` 提供，
 * 内部只碰 `context` 的选中态与 `layouts` 的读取），一次也不会打到写端点。
 * `api.audit()` 就是拿来做这条断言的。
 *
 * 建库引导（向导）也走同一条纪律：三个表单块（建场地 / 建位置 / 建接入机）只调
 * `deps.actions.createSite/createRelay/createGateway/probeGateway`，表单状态是组件自己的
 * `ref`，错误原因就地显示（`store.describe()` 的人话），不借 notice、不碰 api 写口。
 * 三块按层级渐进出现：没场地只有第一块；选了场地出第二块；选了位置出第三块——
 * 顺序即"先建设施、再接节点"的使用逻辑，倒着填会被后端 404/422 拒，不如藏起来。
 */

import { empty } from "./parts.js";

export const plugin_id = "site-tree";

export function create(rt, deps) {
  const { h, ref } = rt;

  return {
    name: "SiteTree",
    props: { widget: { type: Object, required: true } },
    setup(props) {
      const siteName = ref("");
      const siteSlug = ref("");
      const slugTouched = ref(false);
      const relayName = ref("");
      const gwName = ref("");
      const gwUrl = ref("");
      const busy = ref(false);
      const formError = ref(null);
      const probe = ref(null);

      const slugify = (name) =>
        String(name).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

      function say(message) {
        formError.value = { reason: "local", message };
      }

      async function run(fn) {
        if (busy.value) return;
        busy.value = true;
        formError.value = null;
        try {
          await fn();
        } catch (error) {
          formError.value = deps.store.describe(error);
        }
        busy.value = false;
        deps.signal.bump();
      }

      async function submitSite() {
        const name = siteName.value.trim();
        if (!name) return say("场地名先填上");
        const slug = siteSlug.value.trim() || slugify(name);
        if (!slug) {
          return say(`「${name}」里没有能当地代号的英文字符：代号要自己填（小写字母、数字、连字符）`);
        }
        await run(async () => {
          await deps.actions.createSite({ name, slug });
          siteName.value = "";
          siteSlug.value = "";
          slugTouched.value = false;
        });
      }

      async function submitRelay() {
        const name = relayName.value.trim();
        if (!name) return say("位置名先填上");
        await run(async () => {
          await deps.actions.createRelay({ site_id: deps.context.state.siteId, name });
          relayName.value = "";
        });
      }

      async function submitGateway() {
        const name = gwName.value.trim();
        if (!name) return say("接入机名先填上");
        const baseUrl = gwUrl.value.trim();
        await run(async () => {
          const created = await deps.actions.createGateway({
            relay_id: deps.context.state.relayId,
            name,
            base_url: baseUrl || null,
          });
          gwName.value = "";
          // 建完立刻探一次：填了地址就当场给结论（不通也是结论）；没填地址留到树上再探。
          if (baseUrl) probe.value = await deps.actions.probeGateway(created.gateway_id);
        });
      }

      async function probeCurrent() {
        await run(async () => {
          probe.value = await deps.actions.probeGateway(deps.context.state.gatewayId);
        });
      }

      function probeLine() {
        if (!probe.value) return null;
        return h("p", { class: ["probe-line", probe.value.ok ? "probe-line--ok" : "probe-line--bad"] }, [
          h("span", { class: "muted" }, `${probe.value.url} → `),
          probe.value.ok
            ? `通了 · ${probe.value.latency_ms} ms · ${probe.value.dial_state || "未拨号"}`
            : `没通：${probe.value.reason || "原因没说"}${probe.value.detail ? `（${probe.value.detail}）` : ""}`,
        ]);
      }

      function errorLine() {
        return formError.value
          ? h("p", { class: "probe-line probe-line--bad", "data-reason": formError.value.reason }, formError.value.message)
          : null;
      }

      function field(value, placeholder, onInput, cls) {
        return h("input", { class: cls || "wizard-input", placeholder, value: value.value, onInput });
      }

      function wizard(ctx) {
        const blocks = [
          h("fieldset", { class: "wizard-block", key: "site", disabled: busy.value }, [
            h("legend", null, "第一步 · 建场地"),
            h("div", { class: "wizard-row" }, [
              field(siteName, "场地名，如 南山赛场", (e) => {
                siteName.value = e.target.value;
                // 代号跟着名字走，直到用户亲手改过它——那时尊重用户的输入。
                if (!slugTouched.value) siteSlug.value = slugify(e.target.value);
              }),
              field(siteSlug, "代号 site-a（小写字母数字-）", (e) => {
                siteSlug.value = e.target.value;
                slugTouched.value = true;
              }, "wizard-input wizard-input--slug"),
              h("button", { class: "btn", type: "button", disabled: busy.value, onClick: submitSite }, "建场地"),
            ]),
          ]),
        ];
        if (ctx.siteId) {
          blocks.push(
            h("fieldset", { class: "wizard-block", key: "relay", disabled: busy.value }, [
              h("legend", null, "第二步 · 建位置"),
              h("div", { class: "wizard-row" }, [
                field(relayName, "位置名，如 主看台", (e) => { relayName.value = e.target.value; }),
                h("button", { class: "btn", type: "button", disabled: busy.value, onClick: submitRelay }, "建位置"),
              ]),
            ]),
          );
        }
        if (ctx.relayId) {
          blocks.push(
            h("fieldset", { class: "wizard-block", key: "gateway", disabled: busy.value }, [
              h("legend", null, "第三步 · 建接入机"),
              h("div", { class: "wizard-row" }, [
                field(gwName, "接入机名，如 桥 A", (e) => { gwName.value = e.target.value; }),
                field(gwUrl, "base_url，如 http://192.168.99.50:8080", (e) => { gwUrl.value = e.target.value; }),
                h("button", { class: "btn", type: "button", disabled: busy.value, onClick: submitGateway }, "建接入机"),
                ctx.gatewayId
                  ? h("button", { class: "btn", type: "button", disabled: busy.value, onClick: probeCurrent }, "探一下")
                  : null,
              ]),
              h("p", { class: "meta" }, "地址只收局域网（公网/云地址会被拒）；节点从接入机推导，接入机之后才谈板"),
            ]),
          );
        }
        return h("div", { class: "wizard" }, [errorLine(), probeLine(), ...blocks]);
      }

      return () => {
        deps.signal.tick.value;
        const ctx = deps.context.state;
        if (!ctx.sites.length) {
          // 空库引导：S2 的接口通、只是没人建过场地——把第一步直接摆到眼前，而不是只报一句空态。
          return h("div", { class: "tree" }, [
            h("p", { class: "meta" }, "库里还没有场地。从下面这一步开始：场地 → 位置 → 接入机 → 节点"),
            wizard(ctx),
          ]);
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
          wizard(ctx),
        ]);
      };
    },
  };
}

const DIAL_TEXT = { online: "在线", dialing: "拨号中", unreachable: "不通" };
