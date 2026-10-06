# 数据字典

版本：`commerce.v0.1`。所有演示记录为模拟数据，金额采用整数分。

## 1. 通用规则与来源

- JSON使用snake_case；标识为稳定非空字符串，示例M001/P001/O001。主键唯一，外键必须存在。新增数据版本不能重用ID表示不同商品。
- 所有金额使用整数分（`*_cents`），币种首版固定`CNY`；前端才除以100展示元。数值有限，不接受NaN/Infinity、布尔冒充数字、数字字符串。数量/天/月为整数，小时/克/毫秒/测量值为有限小数。`true/false/null`分别表示确认有、确认无、未知。
- 表中“是”表示键和非null值必填；“可空”表示键必须存在，但值允许null。未知不用0、空字符串或猜测值替代。数组`[]`表示确认没有，未知数组用null。可选字段不随意省略，方便客户端识别unknown。
- 真实零值仅在有依据时记录（免运费0、缺货0等）。有线无电池的续航为null且`battery_applicability=not_applicable`，不是0小时。不同口径不得拼接为同一评分。
- 原始数据的来源包括模拟生成、厂商、商家、测评；产品事实最终从数据库读取。LLM不是事实来源。导购/平台类别页不能证明具体型号参数。
- 每个实体有`field_sources`，按字段路径记录来源；叶子字段共享来源可引用同一`Source`内容，但不得只给整条记录一个模糊标签。数组用`battery_specs[0].hours`等路径。主外键、数据版本、updated_at无需单独来源；其他原始叶子都必须有来源，null也要记录采集尝试/未知原因。派生值使用第8节的`derivation`。

| Source字段 | 类型/必填 | 定义、来源与更新 |
| --- | --- | --- |
| source_type | enum/是 | `simulated/manufacturer/merchant/measurement/unknown`；模拟事实必须simulated，不伪造厂商来源 |
| source_ref | string/可空 | URL、文档定位或生成器fixture路径；非unknown时必须非空；simulated示例`demo://small-v1/P001` |
| observed_at | datetime/是 | ISO8601带时区，如`2026-09-28T10:00:00+08:00`；采集或生成时刻，不能写未来 |
| method | string/可空 | 测量/生成方法与条件；measurement和模拟评分必须非空 |
| missing_reason | string/可空 | 原始值null时非空，如`not_collected/not_applicable/not_disclosed`；有值时null |

| 实体公共字段 | 类型/必填 | 口径、来源、更新时点 |
| --- | --- | --- |
| data_version | string/是 | 生成批次版本，来自导入manifest；例如`small-v1`，更新批次时变更 |
| updated_at | datetime/是 | 本条记录最后变更时刻，后端/导入器记录；不能早于相关observed_at |
| field_sources | object<string,Source>/是 | 原始叶子来源映射；字段变更时同步更新 |

演示源文件manifest包含`schema_version`（固定commerce.v0.1）、`data_version`、`profile`（small/integration/demo）、`seed`（整数）、`generated_at`（带时区）、`counts`（merchants/products/offers/product_metrics整数）。每个字段由生成器记录，生成时更新；同种子+同生成器版本+同档位的内容应可复现。生成器版本保存在`generator_version`字符串。重现内容不要求运行时刻相同；时间取manifest明确的固定观测时点。

## 2. Merchant

模拟商家条件由生成器提供并在生成/修改时更新；未来真实商家来源按字段记录。不以authorized推导真伪或保修保证。

| 字段 | 类型/必填 | 单位/范围与口径 |
| --- | --- | --- |
| merchant_id | string/是 | 稳定主键 |
| merchant_name | string/是 | 展示名称；不作为身份匹配键 |
| merchant_type | enum/可空 | `authorized/independent/marketplace` |
| region | string/可空 | 经营地区码，如CN；不表示配送覆盖 |
| merchant_rating | number/可空 | 0–5分，必须含评分方法/平台口径；不能与0–100服务分混用 |
| service_score | number/可空 | 0–100；首版simulated |
| shipping_score | number/可空 | 0–100；首版simulated；不替代具体Offer送达承诺 |
| default_return_policy | ReturnPolicy/是 | 默认退货规则；见第6节；未知规则的叶子为null |
| default_warranty | Warranty/是 | 默认保修规则；见第6节 |

旧需求的`return_policy_days`统一映射为`default_return_policy.days`，不保留第二份独立真值；商家评分与服务评分不同指标，不做无定义换算。

## 3. Product

Product表示确定规格/地区版本的商品本体，不包含商家价格、库存、优惠或退货。源为模拟生成/厂商文档/可核验型号资料；规格、版本或资料变化时更新。需测量的表现放入ProductMetrics，不从名称、蓝牙版本或宣传文案生成真实评分。

| 字段 | 类型/必填 | 单位/范围与口径 |
| --- | --- | --- |
| product_id | string/是 | 稳定主键 |
| identity_key | string/是 | 第7节固定身份组合，唯一 |
| brand | string/是 | 首版虚拟品牌 |
| model | string/是 | 型号代码，不用展示名替代 |
| product_name | string/是 | 展示名 |
| variant | string/是 | 颜色/容量/套装版本代码；未知身份不允许建库 |
| hardware_revision | string/是 | 硬件版本；初版也明确用`rev1`，不把未知版本合并 |
| market_region | string/是 | 地区版本，例如CN |
| category | enum/是 | 仅`headphones` |
| sub_category | enum/是 | `over_ear_headphones/on_ear_headphones/true_wireless_earbuds/wireless_earbuds/gaming_headsets/wired_headphones` |
| form_factor | enum/可空 | `over_ear/on_ear/in_ear/open_ear`；用途分类gaming不决定形态 |
| fit_style | enum/可空 | `headband/ear_tip/ear_hook/ear_clip/neckband` |
| open_ear | boolean/可空 | 不堵耳道的结构说明；与form_factor一致，不表示安全保证 |
| acoustic_design | enum/可空 | `open_back/closed_back/semi_open`；不等同于open_ear |
| wireless | boolean/可空 | 至少一个无线听音模式；不代表每个平台都可无线 |
| connection_modes | ConnectionMode[]/可空 | 逐连接模式的听音、麦克风和供电；[]表示确认无模式，正常可销售商品应至少一个 |
| bluetooth_version | string/可空 | 厂商声明，如5.3；不推导延迟、兼容或多点 |
| active_noise_cancelling | boolean/可空 | 听音主动降噪ANC，不能由ENC代替 |
| microphone | boolean/可空 | 商品具有麦克风；模式能否使用另查ConnectionMode |
| microphone_noise_reduction | boolean/可空 | 麦克风降噪能力，独立于ANC |
| mute_control | boolean/可空 | 是否有明确静音操作 |
| multipoint | boolean/可空 | 支持同时连接多个设备并切换；不等于混合音源 |
| max_active_devices | integer/可空 | ≥1；multipoint=true时至少2；未知不从记忆配对数补出 |
| compatibility | Compatibility[]/可空 | 精确平台/设备/系统/连接模式和固件条件，见下表 |
| battery_applicability | enum/是 | `applicable/not_applicable/unknown` |
| battery_specs | BatterySpec[]/可空 | 单次/含盒和ANC条件分别记录，见下表 |
| charging_methods | string[]/可空 | 已确认充电方式，如usb_c/qi；充电口不表示音频输入 |
| fast_charge | FastCharge/可空 | 充电分钟与增加播放时间的配对条件 |
| weight_specs | WeightSpec[]/可空 | 单只/双只/整副/含盒，见下表 |
| foldable | boolean/可空 | 是否能折叠，不从便携评分推导 |
| ear_tip_sizes | string[]/可空 | 随商品附带的可替换耳塞套规格，如S/M/L；[]为确认无 |
| earbud_dimensions_mm | Dimensions/可空 | 单只外壳尺寸，不包括耳塞套；不保证适合某耳道 |
| water_resistance | Protection[]/可空 | 保护对象与厂商等级分别记录 |
| sweat_use_statement | SweatStatement/可空 | 运动汗液适用声明与条件；单有IPX不足以通过 |
| codec_support | string[]/可空 | 支持的编码；实际可用模式查compatibility |
| description | string/可空 | 展示说明，不作为自动筛选证据 |
| image_path | string/可空 | 本地静态资源路径；有可用图片时更新，不要求网络图 |

### 3.1 连接和兼容结构

| 结构.字段 | 类型/必填 | 口径 |
| --- | --- | --- |
| ConnectionMode.mode_id | string/是 | 商品内唯一；供续航、兼容、测量关联 |
| ConnectionMode.transport | enum/是 | `wired_3_5mm/usb_audio/bluetooth/wireless_dongle/optical_transmitter` |
| ConnectionMode.audio_connector | string/可空 | 接头协议与规格，如3.5mm_TRRS_CTIA，不只外形 |
| ConnectionMode.microphone_connector | string/可空 | 麦克风接头或协议；不适用记null并说明 |
| ConnectionMode.playback_supported | boolean/可空 | 听音是否支持 |
| ConnectionMode.microphone_supported | boolean/可空 | 该模式麦克风是否支持 |
| ConnectionMode.active_noise_cancelling | boolean/可空 | 该模式听音ANC是否可用；不能由商品总能力继承为true；被动有线常与蓝牙能力不同 |
| ConnectionMode.requires_power | boolean/可空 | 听音是否需要耳机/配件供电；SC06须为false |
| ConnectionMode.required_accessory_codes | string[]/可空 | 必要适配器/发射器型号代码；[]明确无需 |
| Compatibility.compatibility_id | string/是 | 商品内唯一 |
| Compatibility.device_key | string/是 | 精确目标设备型号，或有证据覆盖的接口/平台代码 |
| Compatibility.platform | string/是 | 如windows/ps5/tv，不单独决定匹配 |
| Compatibility.os_version | string/可空 | 适用系统版本范围；null不代表全部支持 |
| Compatibility.source_output | string/可空 | 目标设备输出协议/接口，例如optical_pcm |
| Compatibility.mode_id | string/是 | 引用同商品ConnectionMode |
| Compatibility.playback_supported | boolean/可空 | 指定设备与模式听音能力 |
| Compatibility.microphone_supported | boolean/可空 | 指定设备与模式语音能力 |
| Compatibility.multipoint_supported | boolean/可空 | 此设备/模式多点能力 |
| Compatibility.firmware_version | string/可空 | 适用固件范围；null需明确资料未限制或未披露 |
| Compatibility.conditions | string/可空 | 必须满足的前置条件；无法结构化确认时澄清，不自动判定通过 |

兼容筛选要求用户提供目标设备/接口和所需听音/语音模式，并与来源覆盖范围匹配；无法确定映射时返回澄清。不能凭字符串相似、品牌名或蓝牙5.x推断兼容。

### 3.2 续航、重量和防护结构

| 结构.字段 | 类型/必填 | 单位/口径 |
| --- | --- | --- |
| BatterySpec.battery_spec_id | string/是 | 商品内唯一 |
| BatterySpec.hours | number/可空 | 小时，>0；未知为null |
| BatterySpec.basis | enum/是 | `single_charge/with_case`；含盒累计不满足连续听音 |
| BatterySpec.anc_state | enum/是 | `on/off/not_applicable/unknown`；unknown不通过ANC开启续航硬条件 |
| BatterySpec.mode_id | string/可空 | 对应连接模式；未知不用于特定模式硬筛选 |
| BatterySpec.test_conditions | string/可空 | 音量、编码、通话/听音、测量/声明条件；模拟明确方法 |
| WeightSpec.weight_spec_id | string/是 | 商品内唯一 |
| WeightSpec.grams | number/可空 | 克，>0 |
| WeightSpec.basis | enum/是 | `single_earbud/pair/headset/with_case`；headset指整副头戴，含线与否由conditions注明 |
| WeightSpec.conditions | string/可空 | 耳塞套、线缆/配件等范围；未知不作为已满足重量上限 |
| FastCharge.minutes | integer/是 | 充电时长，>0分钟 |
| FastCharge.playback_hours | number/是 | 增加的播放时长，>0小时，不替代单次续航 |
| FastCharge.anc_state | enum/是 | 与BatterySpec相同枚举 |
| FastCharge.conditions | string/可空 | 测量/声明条件 |
| Dimensions.length/width/height | number/可空 | 各为>0毫米；逐轴来源，缺一轴不补造 |
| Protection.component | enum/是 | `earbuds/headset/case`；耳机与盒分别记录 |
| Protection.rating | string/可空 | 如IPX4；字符串不是防汗证据 |
| Protection.conditions | string/可空 | 厂商使用限制，不能保证水下使用 |
| SweatStatement.allowed | boolean/可空 | 明确允许运动汗液使用；null或无声明不能通过硬要求 |
| SweatStatement.conditions | string/可空 | 适用运动/维护/限制；必须可核验才能宣称符合 |

没有独立裸值`battery_hours/weight_g`：API输出完整规格数组及选中spec_id。旧字段映射到这些规格记录，不能在生成/排序时混用。S2.2按形态定义归一化；仅同basis、ANC条件、模式与可比条件比较，不跨basis直接比克或小时。不能凭单只重量乘2生成双只实测值。

## 4. ProductMetrics

每Product恰好一条指标记录（未知也可空）；product_id为主键/外键。模拟评分全部标simulated，0–100；真实评分只有来源、方法、范围、测试条件可比较时才进入后续算法。指标于生成或新测评/统计批次更新，不能把网页导购的用途归类当实测。

| 字段 | 类型/必填 | 单位/范围与口径 |
| --- | --- | --- |
| product_id | string/是 | 引用Product |
| rating | number/可空 | 0–5用户评价均分；不同平台均分不静默混合 |
| review_count | integer/可空 | ≥0条；确认0评价时rating为null |
| return_rate | number/可空 | 0–1比例，method注明周期及分母；不由退货天数推算 |
| popularity_score | number/可空 | 0–100；模拟热度，初版不默认进入排序 |
| quality_score | number/可空 | 0–100；需有方法，不代替音质或可靠性保证 |
| comfort_score | number/可空 | 0–100；模拟/测试群体与佩戴条件，非个体不夹耳承诺 |
| anc_score | number/可空 | 0–100；ANC能力表现，功能不存在时不虚构测试；null不等于功能false |
| sound_quality_score | number/可空 | 0–100；特定评价/模拟方法，不等同音色偏好匹配 |
| music_score | number/可空 | 0–100；听音用途指标，保留定义；S2.2不得与sound_quality重复计权 |
| call_score | number/可空 | 0–100；指定设备、噪声与模式下通话/模拟表现 |
| commute_score | number/可空 | 0–100；模拟通勤用途指标，不证明安全 |
| gaming_score | number/可空 | 0–100；游戏用途指标，不等同延迟实测 |
| portability_score | number/可空 | 0–100；模拟携带便利度 |
| fit_stability_score | number/可空 | 0–100；模拟/测评佩戴稳定，非永不掉落承诺 |
| measurements | Measurement[]/可空 | 延迟、漏音、频响/音色等额外证据；空或null不支持性能比较 |

| Measurement字段 | 类型/必填 | 口径 |
| --- | --- | --- |
| measurement_id | string/是 | 商品内唯一 |
| metric | enum/是 | `latency_ms/leakage_db/frequency_response/tone_profile` |
| value | number或string/可空 | 前两项有限非负数；后两项为证据摘要字符串，不能据此自动打分 |
| unit | enum/是 | 分别`ms/dB/report`；频响/音色均report |
| mode_id | string/可空 | 引用Product模式；特定模式测量不得泛化 |
| conditions | string/是 | 延迟注明设备/系统/编码/模式/测法，漏音注明音量/距离/环境，报告注明方法 |
| comparison_group | string/是 | 可比实验协议ID；不同组不比较数值，不把缺测当0延迟/0漏音 |

旧`microphone_quality`统一用call_score与其来源方法表达，`game_score`统一为gaming_score；`noise_cancelling`统一为active_noise_cancelling（ANC），不保留含糊别名作为事实字段。

## 5. Offer 与到手价

源为商家报价/库存/条款或模拟生成；观察报价、库存或政策变更时更新，首版为固定快照，不声称实时。Product与Merchant同批次存在；同批次(product_id,merchant_id)唯一。报价固定对应identity_key，不混入另一个地区/颜色/型号；商品全新/二手状态在Offer另列。

| 字段 | 类型/必填 | 单位/范围与口径 |
| --- | --- | --- |
| offer_id | string/是 | 稳定主键 |
| product_id | string/是 | 引用确定规格Product |
| merchant_id | string/是 | 引用Merchant |
| currency | enum/是 | 仅CNY |
| price_cents | integer/是 | ≥0，当前商品售价（已含直接降价与税），不再次扣discount_rate |
| commission_rate | number/兼容性可选 | S2.2扩展：有限0–1比例，仅用于显式platform_bias佣金曝光实验；未知null，旧commerce.v0.1批次可缺键，存在时须标准field_sources。新生成批次默认null，不从商家类型/价格虚构佣金；正常Offer排序不使用此值 |
| original_price_cents | integer/可空 | ≥price_cents，已核验的划线参考价；未知null |
| shipping_fee_cents | integer/可空 | ≥0；对delivery.destination_region明确，未知总价不能算0运费 |
| promotion | string/可空 | 展示说明；不参与自由文本价格计算 |
| instant_discount_cents | integer/是 | ≥0且≤price_cents；当前用户无需资格判断可用的额外优惠；无优惠为0，不重复扣直接降价 |
| accessories | Accessory[]/可空 | 当前模式需要的完整配件方案；[]明确无需，未知null |
| stock | integer/可空 | ≥0套完整方案；0缺货；null未知，不作为有货推荐 |
| item_condition | enum/可空 | `new/refurbished/used`；未指定全新不自动加条件 |
| delivery | Delivery/是 | 目的地和送达范围；叶子可空，见下表 |
| return_policy_override | ReturnPolicy/可空 | null继承商家完整默认；非null为完整替代对象，不按单字段null继承 |
| warranty_override | Warranty/可空 | 同上，完整替代，null叶子仍为未知 |
| gift_return_conditions | string/可空 | 礼物退货附加说明；无法确认的硬要求需要澄清 |
| valid_until | datetime/可空 | 报价有效期；非null且过期不得推荐；null只代表未给截止，不保证实时有效。非null值必须为带时区的ISO8601（N1，2026-09-30）：查询端按parse_iso解析，非法/无时区值在校验器受控拒绝 |

| Accessory字段 | 类型/必填 | 口径 |
| --- | --- | --- |
| accessory_code | string/是 | 匹配ConnectionMode必要配件代码 |
| included | boolean/是 | 已包含于price_cents的配件true，否则false |
| price_cents | integer/可空 | included=true时固定0；false时为同Offer确定可购价格，≥0 |
| stock | integer/可空 | ≥0；非包含配件必须>0才组成合格方案 |
| delivery_in_same_package | boolean/可空 | 截止硬要求必须true，否则首版无法验证完整方案到货 |

配件只能描述同商家同Offer可核验的完整方案；不跨商家自动拼单。配件兼容代码、价格、库存、配送缺失时，不确认该模式的预算/兼容/截止条件。包含配件库存以整套stock为准。预算比较所需配件固定在选中模式上；切换模式必须重算资格和到手价。

`landed_price_cents = price_cents + shipping_fee_cents + accessory_total_cents - instant_discount_cents`。`accessory_total_cents`为选中模式必要且不包含的配件价合计，不为不必要配件收费；任何必要项未知则总价null。退货运费为决策成本另列，不计入购买到手价。优惠不模拟会员/券/分期资格；缺货最低价不得参与展示价或预算筛选。

`discount_rate`仅派生：(original_price_cents-price_cents)/original_price_cents，0–1；原价null或0时null。原价、降价比例不用于到手价。商品不存静态`price_band`，区间由当前有货完整Offer到手价筛选。

| Delivery字段 | 类型/必填 | 口径 |
| --- | --- | --- |
| destination_region | string/可空 | 固定配送地区码，首版默认CN-SH；不代表全国 |
| min_days/max_days | integer/可空 | 各≥0自然日，自observed_at当地日期起计到送达（不是发货）；min≤max。校验器要求各端点在配送来源observed_at（转Asia/Shanghai日期）上加法可计算（N2，2026-09-30）；既有异常数据的查询映射为数据错误而非内部错误 |
| commitment_type | enum/是 | `estimated/guaranteed/unknown`；保证必须有可核验条款，即使模拟也标simulated |
| conditions | string/可空 | 截单、地区等适用条件；无法确定符合时不通过硬截止 |

配送的所有叶子来源observed_at必须相同，作为delivery_window.observed_at；不同观测批次先核对/刷新，不能混成一条承诺。送达窗口派生为该observed_at转Asia/Shanghai日期+min/max_days，含当天day0；窗口仅用于该观测日期下单的方案，不拿历史日期保证今天下单到货。首版静态快照跨日时，该Offer的配送窗口不能通过硬截止；返回无合格报价/STALE_DATA，需要刷新数据，而非滚动旧日期。无硬截止的传统展示仍可保留未超过valid_until的报价，明确展示观测时点和历史窗口，不宣称当前必达。API将observed_at与窗口一起展示。用户给出相对截止日期时，以请求本地时间转绝对日期并展示；未给地区/是否接受预计需澄清。截止要求在同一Offer验证max到货日期、地区、条件、配件和commitment_type；未知不通过。旧shipping_days对应max_days，不能解释为必达。

## 6. 有效退货与保修

ReturnPolicy、Warranty每个叶子都有商家/报价条款或模拟来源。Offer的非null覆盖对象整体优先；未知叶子不得继承默认最佳值。有效对象及来源由后端派生，API同时提供原始覆盖与有效条件，以便核对。

| ReturnPolicy字段 | 类型/必填 | 口径 |
| --- | --- | --- |
| days | integer/可空 | ≥0日，从签收日起计；0明确无退货期，null未知 |
| opened_return_allowed | boolean/可空 | 是否明确允许按条款开封试戴退货；null不符合SC14 |
| return_shipping_fee_cents | integer/可空 | ≥0分，固定场景下买家承担的运费；未知不宣称免费退货 |
| conditions | string/可空 | 开封、包装、卫生/功能、申请条件；未结构化确认时不能保证硬要求通过 |
| exceptions | string[]/可空 | 商品/报价例外；[]确认无例外，null未知 |

| Warranty字段 | 类型/必填 | 口径 |
| --- | --- | --- |
| months | integer/可空 | ≥0月，从签收起；0明确不保修 |
| provider | string/可空 | 实际保修责任主体，不从商家类型推断 |
| conditions | string/可空 | 覆盖/凭证/区域条件；未知不按最佳保修评分 |

有效政策不会消灭限制：即使days=30，opened_return_allowed=false，也不能通过“允许开封试戴退货”。文字条件影响资格但不能由LLM自行宣布已满足；数据库按场景核验明确条件，无法判定时排除或澄清。有效政策`policy_origin`为`merchant_default/offer_override`；其field_sources逐字段沿用实际生效来源，不将继承变成实测。

## 7. 商品身份及关联

`identity_key`由规范化后的(brand, model, variant, hardware_revision, market_region)序列化成JSON数组字符串。规范化只去首尾空白、Unicode NFC、品牌/代码按预先定义的大小写规则（本版统一小写）；不删除有意义字符、不模糊匹配营销名。数据库对identity_key加唯一约束。

- 同身份只建一个Product，多商家Offer引用同product_id；报价导入必须带完整身份并验证与该ID相同。字段冲突进入人工核对，不择优拼参数。
- 颜色、硬件/区域或套装规格不同建不同Product。配件补购方案是Offer的额外必要配件，不得冒充商品标准套装版本。
- 固件不是新Product的默认理由；兼容记录明确适用固件并保留历史来源。硬件修订则新身份。
- 不能仅以名称、品牌+系列、近似型号关联。未确认版本的真实报价进入待核验队列，不写正常演示数据。
- ProductMetrics一对一；Offer多对一关联Product/Merchant。合法未知与非法数据分开：负价、孤立Offer、身份冲突等只放测试fixtures。

## 8. 派生展示与硬/软条件

后端派生值带`derivation={rule:string,input_refs:string[]}`；rule是本节稳定规则代码，input_refs用`O001.shipping_fee_cents`等可追溯路径。派生对象的field_sources保存所用原始叶子来源；不为派生计算伪造manufacturer来源。

| 派生字段 | 类型/可空 | 规则代码与输入 |
| --- | --- | --- |
| landed_price_cents | integer/是 | `landed_price.v1`；第5节公式，模式/必要配件决定输入；未知总价null |
| accessory_total_cents | integer/是 | `accessory_total.v1`；所选模式必需配件求和 |
| discount_rate | number/是 | `discount_rate.v1`；不用于预算 |
| delivery_window.earliest/latest | date/是 | `delivery_window.v1`；观测日+min/max_days；任一分量未知则该端点null |
| effective_return_policy | ReturnPolicy/否 | `return_policy.v1`；完整覆盖优先，policy_origin及来源一起输出 |
| effective_warranty | Warranty/否 | `warranty.v1`；完整覆盖优先，同上 |
| display_offer_id | string/是 | `display_offer.v1`；有货完整报价中最低到手价，平手按offer_id升序；无完整报价null |
| display_price_cents | integer/是 | `display_offer.v1`；来自display_offer_id，不把最低缺货价显示成可购价 |
| availability | enum/否 | `availability.v1`；有可定价有货Offer为in_stock；全部stock=0为out_of_stock；其余unknown |

传统默认展示模式是Product.connection_modes按mode_id升序第一个playback_supported=true且完整方案可定价的模式，并输出mode_id；Agent按用户连接需求另选模式，返回相应到手价。传统地区默认CN-SH，有地区不符/过期/价格未知的报价不能当作display_offer。展示价、价格筛选和价格排序必须复用同一display_offer规则。

硬约束的输入字段null、来源缺失或证据范围不符都不视为满足；缺少用户设备等必要输入时请求澄清。每个Product须至少一个Offer在同一模式下同时满足预算、库存、目的地、时间、售后等要求。禁止从便宜缺货Offer取价、从贵Offer取库存再拼合。未给出的条件不自动增加，Agent默认stock_required=true；真实示例均用有标记的模拟证据演示。

选中模式的能力必须单独验证：wireless=true要求所选transport为bluetooth/wireless_dongle（optical_transmitter须另有明确无线传输证据，本版不自动视为无线）；wireless=false要求wired_3_5mm/usb_audio。双模商品可用有线方案满足“不用无线”，不能因Product.wireless=true整款拒绝。active_noise_cancelling要求查ConnectionMode同名字段，microphone要求模式及目标设备的microphone_supported；未知不通过。商品级true仅表示至少一种模式具备能力，不能给另一个模式继承。multipoint、续航与配件同样绑定实际模式和设备条件。推荐模式切换后全部重新核验；API的selected_specs明确所用续航/重量spec_id（未使用为null）。

S2.1条件核验实现（2026-09-30整改）：仅一个连接模式时，商品级多点与同时连接台数可定位到该模式；多模式的多点硬要求须有目标设备兼容证据，未提供设备时按未知排除。Compatibility.multipoint_supported=true只证明至少2台，不证明多模式商品的某一模式支持3台以上。意图multipoint=false与设备multipoint_required=true返回澄清，不择一执行。BatteryRequirement.conditions非null时须与所选同模式规格的非空test_conditions精确相等，不能猜测文本等价；不匹配或无法确认按未知排除。重量conditions为null/空白时不证明硬上限。

Compatibility.conditions本版仅实现固定模拟语料的可核验规则，精确匹配完整说明，不作关键词放行：USB直连说明要求usb_audio模式；QG-DONGLE-USB适配器说明（含“同PS5”说明）要求wireless_dongle及该模式必需配件包含QG-DONGLE-USB；电视光纤说明要求用户source_output=optical_pcm、optical_transmitter及必需配件VK-TX-OPT；PC TRRS说明要求用户device_key明确为“通用PC（3.5mm TRRS复合口）”且模式audio_connector=3.5mm_TRRS_CTIA。配件是否具备、库存和成本仍由同一Offer的build_plan核验。conditions未知或任何新说明均按未知排除，不自动保证兼容；扩展语料前须补结构化核验规则与回归。此规则可能返回零候选，上游解析器可在缺设备/接口输入时请求澄清。


- Offer.delivery.conditions且commitment_type=guaranteed的“模拟次日达服务条款（工作日18点前下单）”：请求时间（Asia/Shanghai本地时间）须为周一至周五且早于18:00；不满足即该报价不满足guaranteed承诺类型及硬截止，排除并给出原因，不顺延、不重算窗口。工作日不计法定节假日，注明为仿真简化。
- Offer.delivery.conditions在estimated（或承诺类型未知）下的“模拟自营配送”“模拟第三方物流”“模拟华东仓发货”“模拟经济快递”：说明性文字，不影响资格。
- opened_return_allowed=true的退货政策conditions中“包装和配件齐全”“配件齐全且包装完好”“包装配件齐全，支持开封试戴”属退货时买家义务，放行；“支持开封试戴后按条款退货”（O015覆盖）引用未给出的条款，按未知排除。opened_return_allowed=false的条件（如“仅未开封”“不支持退货”）本身已不满足开封退货要求，无需另立规则。**有效退货政策exceptions同时核验（S2AR-01，2026-10-01整改）：[]为确认无例外、放行；null（未知）与非空例外清单（如“仅经核实的付费会员可申请开封退货”，本版无可核验语料）均按未知排除——有效政策的限制不能因opened=true而消失，商家默认与Offer覆盖两条来源路径同规则。**
- SweatStatement.conditions“模拟厂商声明：运动汗液适用，训练后需擦拭晾干”为维护义务，通过普通防汗要求；“模拟厂商声明：室内训练防汗适用，训练后需清洁”同样通过普通防汗要求，其“仅室内训练”局限须由解释层在不足项如实列出，日后意图加入户外场景时再排除。

上述门槛仅在用户表达对应硬要求（配送承诺类型/条件/硬截止、开封退货、防汗）时执行；需求侧未表达不能消除报价自身条款。硬要求可能因此返回零候选，上游解析器可请求澄清，不得自行放宽。另：ConnectionMode.required_accessory_codes区分null与[]（2026-10-01整改，S2A-01）——null/缺失表示必要依赖、费用与库存未知，方案不完整、landed_price不可算；显式[]才按无需配件处理（accessory_total=0）。

软指标null保持unknown；S2.2为每个维度定义固定中性处理并展示缺失，不能直接填原始数据0或100。模拟评分0–100不等于真实满意概率；不同method/comparison_group的真实指标不得直接共用分数。Product与Offer权重分开，最终价格分必须对应最终推荐Offer。

