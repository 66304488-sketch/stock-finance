(function () {
  'use strict';

  var guides = {
    'sentiment-radar.html': {
      title: '板块情绪雷达使用说明',
      subtitle: '把极端位置、压力衰竭和反向确认拆开，分别观察反弹与退潮',
      html: `
        <div class="page-guide-flow"><span><b>01</b>选择行业分类</span><span><b>02</b>看情绪位置</span><span><b>03</b>确认所处阶段</span><span><b>04</b>点击查看成分股</span><span><b>05</b>比较T1—T5频率</span><span><b>06</b>检查反对证据</span></div>
        <div class="page-guide-grid">
          <section><h4>为什么不是一个情绪分</h4><p>情绪位置只描述板块处于恐慌还是亢奋，不直接预测反转。极弱板块可能继续下跌，极强板块也可能继续上涨，因此页面将底部反弹与顶部退潮拆成两个条件事件。</p><p class="page-guide-formula">反转证据 = 先前极端位置 + 原方向压力衰竭 + 反方向价格/广度/成交确认</p></section>
          <section><h4>情绪位置如何计算</h4><p>位置范围为 −100 至 +100：近5日相对市场收益的自身历史分位占50%，价格延伸分位占25%，当日涨跌家数广度占15%，方向成交差率占10%。负值偏恐慌，正值偏亢奋。</p><p>位置是描述变量；表格排序优先使用阶段和反向确认，不按绝对情绪值机械抄底或逃顶。</p></section>
          <section><h4>底部反弹阶段</h4><ul><li><b>接飞刀风险：</b>近5日弱势且价格低位，但卖压没有衰减、价格/广度/方向成交尚未共同转正。</li><li><b>卖压衰竭：</b>高成交努力无法继续压低价格，或新低覆盖从极值收缩。</li><li><b>反弹确认：</b>当日价格、上涨家数广度和方向成交差率同时转正；活跃广度转正、成交不过度集中可提高质量。</li></ul></section>
          <section><h4>顶部退潮阶段</h4><ul><li><b>亢奋观察：</b>近5日强势、价格延伸高，但内部扩张尚未破坏。</li><li><b>上涨衰竭/派发预警：</b>成交仍热但价格响应下降，或新高覆盖开始收缩。</li><li><b>退潮确认：</b>此前亢奋后，价格、下跌家数广度和方向成交差率共同转负。</li></ul></section>
          <section><h4>板块个股如何阅读</h4><p>点击候选榜或行业雷达表中的任一行业，会在详情首屏加载当前分类下的全部成分股。个股行情严格截断到雷达信号日；同花顺缺失分类回退申万一级，申万三级缺失时依次回退二级和一级。</p><p>“内部角色”用当日涨跌和20日量比区分放量领涨、反弹先锋、主要拖累、放量领跌与放量分歧；“成交影响”等于当日涨跌幅乘板块内成交额占比，只解释谁在推动或拖累板块交易，不是行业指数权重贡献。点击名称可跳转同花顺复核。</p></section>
          <section><h4>T1—T5与概率口径</h4><p>信号在收盘后确定，下一交易日开盘进入，扣除20bp成本后分别观察T1至T5。普通反转要求绝对收益与相对全市场收益方向均正确；强反转还要求横截面进入前/后30%。</p><p>当前显示2025-05-14至2026-08-04固定样本的历史条件频率、基础频率和提升倍数，不是完整walk-forward校准概率。申万三级按有效成分数以κ=20向申万一级同类条件收缩。</p></section>
          <section><h4>如何判断危险</h4><p>优先比较当前频率与同类基础频率，而不是只看是否超过50%。同时检查独立日期、95%区间、强反转率和平均T5超额；命中率提高但平均超额未改善时只作观察。</p><p class="page-guide-warning">当前行业映射回溯历史存在分类前视，同花顺存在回退分类，板块收益使用成分股等权研究代理。任何频率都不能替代仓位、止损、流动性和可交易载体检查。</p></section>
        </div>`
    },
    'opportunity-summary.html': {
      title: '机会汇总使用说明',
      subtitle: '用独立证据交叉验证机会，并把风险、冲突和载体分开',
      html: `
        <div class="page-guide-flow">
          <span><b>01</b>过数据闸门</span><span><b>02</b>看市场许可</span>
          <span><b>03</b>找行业触发</span><span><b>04</b>核对独立确认</span>
          <span><b>05</b>检查风险与载体</span>
        </div>
        <div class="page-guide-grid">
          <section>
            <h4>汇总逻辑</h4>
            <p>汇总不是把所有分数相加。数据质量先决定能否判断；市场参与强度决定是否允许承担风险；行业新高/新低扩散负责触发；价格、成交参与、结构和直接需求负责独立确认；拥挤与失效证据可以降级或否决。</p>
            <p class="page-guide-formula">数据闸门 → 市场许可 → 行业触发 → 独立确认 → 风险否决 → ETF / 个股载体</p>
          </section>
          <section>
            <h4>为什么按证据域计票</h4>
            <p>同一份行情可派生出新高宽度、净扩散和加速度，但它们仍属于一个“触发域”，不能伪装成三份独立证据。可手动切换申万一级、同花顺和申万三级观察结果是否稳健；当前不做跨口径自动计票。</p>
            <p>详情会披露每个证据的来源页、日期、方向和状态；缺失保持未知，不按零值处理。</p>
          </section>
          <section>
            <h4>三类候选</h4>
            <ul>
              <li><b>已确认：</b>行业触发后，价格、成交参与、市值结构、直接需求四个非触发域中至少两个独立跟随，且没有风险否决；是否可执行仍要再过市场许可、数据质量和载体闸门。</li>
              <li><b>待确认：</b>已有触发或相对强势，但证据尚不完整；详情会明确列出缺失项与冲突。</li>
              <li><b>已否决：</b>拥挤、量价失效、流动性脆弱或数据质量触发否决，仅保留研究记录。</li>
            </ul>
            <p>候选允许为空；没有通过闸门的对象不会为了凑数进入榜单。</p>
          </section>
          <section>
            <h4>风险、冲突与质量</h4>
            <p>拥挤集中不自动等于反转，但若同时出现内部宽度恶化、量价低效率、流动性脆弱或价格破坏，会升级为否决。来源日期错位、覆盖不足和盘中快照过期会降低质量或暂停判断。</p>
            <p class="page-guide-warning">机会分只用于同口径排序，不是上涨概率；风险和质量不会被机会分抵消。</p>
          </section>
          <section>
            <h4>ETF 与个股载体</h4>
            <p>ETF 按代码去重，并展示行业映射、需求或相对强度证据；没有通过筛选时可为空。个股来自触发或确认中的代表性明细，只用于解释行业由谁驱动。</p>
            <p>点击股票可打开同花顺对应个股；执行前仍需核对流动性、价格位置和自身风险约束。</p>
          </section>
          <section>
            <h4>建议阅读顺序</h4>
            <ol>
              <li>确认“截至日期”、模式和闸门是否通过。</li>
              <li>看市场许可，再选一类候选。</li>
              <li>展开证据链，确认至少两个来源域独立。</li>
              <li>先读冲突、风险和失效条件，再看载体。</li>
              <li>点击来源回到原始页面复核。</li>
            </ol>
          </section>
        </div>`
    },
    'industry-heatmap.html': {
      title: '行业机会热力图使用说明',
      subtitle: '从市场许可到行业状态，再回到原始日期矩阵验证',
      html: `
        <div class="page-guide-flow">
          <span><b>01</b>看数据闸门</span><span><b>02</b>看市场许可</span>
          <span><b>03</b>筛状态迁移</span><span><b>04</b>看风险与失效</span>
          <span><b>05</b>用下方原始矩阵复核</span>
        </div>
        <div class="page-guide-grid">
          <section>
            <h4>页面解决什么问题</h4>
            <p>创新高/新低本质上是市场尾部参与宽度，不是单纯的涨跌榜。机会视图把行业从“沉寂→萌芽→确认→延续”或“拥挤→衰退”分类；原始矩阵保留每天、每个行业的完整变化，两者应结合使用。</p>
            <p><b>市场许可：</b>进攻表示全市场新高扩散占优；观察表示方向证据不一致；防守表示新低或下跌广度占优，只寻找相对强势；暂停表示数据不具可比性，停止排序。</p>
          </section>
          <section>
            <h4>宽度与扩散如何计算</h4>
            <p class="page-guide-formula">原始净扩散率 = (创新高家数 − 创新低家数) ÷ 行业有效成分数 × 100%</p>
            <p>为防止申万三级等小行业被一两只股票放大，机会表采用 κ=20 的市场先验收缩：高/低覆盖率分别按 <span class="page-guide-code">(行业家数 + κ×市场覆盖率) ÷ (行业总数 + κ)</span> 计算，再相减。表格同时保留原始家数，便于识别收缩影响。</p>
            <p><b>扩散加速度：</b>当前收缩后净扩散减去前 3 个有效交易日均值；分位只与此前可用历史比较，避免使用未来数据。</p>
          </section>
          <section>
            <h4>确认项与状态</h4>
            <ul>
              <li><b>相对趋势：</b>行业收益相对全市场的标准化历史分位，且超额收益为正才确认。</li>
              <li><b>成交参与：</b>行业成交活跃相对自身历史的因果分位，不是主力净流入。</li>
              <li><b>活跃广度：</b>同方向异常成交股票占有效成分的比例；有效参与者用 <span class="page-guide-code">(Σw)²/Σw²</span> 折算，少数龙头独占时会下降。</li>
              <li><b>拥挤/衰竭：</b>价格延伸、成交集中、量价低效率等独立危险证据出现时优先标记，不能因总分高而隐藏。</li>
            </ul>
          </section>
          <section>
            <h4>盘中口径</h4>
            <p><b>触及</b>表示价格曾越过历史阈值；<b>严格保持</b>表示当前价格仍越过同一条历史高/低阈值。盘中净扩散只使用严格保持家数。</p>
            <p>每 5 分钟曲线显示新增触及，高低共用同一刻度。上一收盘日成交动能只作风险背景，不参与盘中确认，因此“盘中观察”不是可执行确认信号。</p>
          </section>
          <section>
            <h4>原始矩阵怎么读</h4>
            <p>颜色是该行业当天覆盖率在自身历史中的分位 P，不按全市场绝对家数染色；单元格同时显示家数、覆盖率和 P。0 是有效零值，斜纹“缺”表示质量检查未通过。</p>
            <p>先看连续多日由冷转热，再看是否伴随新低收缩；单日突然变红但次日消失通常只是脉冲。点击单元格可下钻到个股。</p>
          </section>
          <section>
            <h4>轮动象限怎么读</h4>
            <p>横轴是 κ=20 市场先验收缩后的净扩散率，坐标范围随当前分类的最大绝对值向上取整，避免申万三级行业被固定轴裁到边界。纵轴是行业成交参与相对自身历史的因果分位减 50，P50 为中性。机会表中点击行业名可直接打开该行业最新一期创新高个股列表；点击行内其他位置仍查看完整机会证据。</p>
            <p>右上表示扩散且成交参与确认；右下表示扩散但参与不足；左上表示收缩伴随高参与，通常是主动杀跌或高风险；左下表示共同沉寂。圆面积近似代表当日成交规模，黄圈表示存在独立风险证据，红圈表示已判定拥挤。</p>
          </section>
          <section>
            <h4>周期、操作与误区</h4>
            <p>20/60/120 日用于短中期结构，一年和历史用于长期位置；自定义输入的是交易日数。申万一级、同花顺和申万三级会整页同步切换。</p>
            <p class="page-guide-warning">排序分只用于比较证据，不是上涨概率。样本不足时不显示成功率；“新高很多”也可能处于末端拥挤，必须同时检查市场许可、成交参与、风险和失效条件。</p>
          </section>
        </div>`
    },
    'market-temperature.html': {
      title: '市场参与强度使用说明',
      subtitle: '用宽度判断有多少股票参与，而不是预测指数明天涨跌',
      html: `
        <div class="page-guide-flow"><span><b>01</b>确认日线/盘中口径</span><span><b>02</b>看当前分档</span><span><b>03</b>看 5 日方向</span><span><b>04</b>与指数交叉验证</span></div>
        <div class="page-guide-grid">
          <section><h4>核心定义</h4><p>参与强度是 0–100 的历史状态分数。每个因子先与此前最多 250 个交易日比较得到因果分位，再按可用权重加权；缺失因子会从分母中剔除，不按 0 处理。</p><p class="page-guide-formula">参与强度 = Σ(因子历史分位 × 权重) ÷ Σ可用权重</p></section>
          <section><h4>收盘六因子</h4><ul><li>涨跌宽度 30%：<span class="page-guide-code">(上涨家数−下跌家数)/有效股票数</span></li><li>涨跌停差 20%：涨停家数−跌停家数，阈值按主板、ST、创业/科创、北交所近似。</li><li>60 日新高−新低 25%</li><li>成交活跃 10%：全市场成交额÷20 日均额</li><li>方向成交代理 7.5%；市值涨跌 7.5%</li></ul></section>
          <section><h4>盘中估算</h4><p>交易时段只使用可同步得到的三因子：涨跌宽度 35%、涨跌停差 30%、新高新低 35%。三者都与收盘历史分布比较。日期不匹配或覆盖不足时会降级，收盘后切回稳定日线。</p></section>
          <section><h4>曲线和分档</h4><p>“较昨日”看边际变化，“5 日状态”看方向是否持续，分档持续天数用于判断环境是否刚切换。60/120/250 日只是显示区间，不改变当日计算。</p><p>指数上涨而参与度下降是权重托举；指数下跌但参与度回升可能是内部修复，两者都需要后续确认。</p></section>
          <section><h4>建议用法</h4><ol><li>先确认数据日期与置信度。</li><li>看强度是在上升、横盘还是下降，而非只看绝对值。</li><li>再去热力图找扩散来源，去拥挤页检查退出风险。</li></ol></section>
          <section><h4>边界</h4><p class="page-guide-warning">极弱不自动等于抄底，极强也不自动等于卖出。该页描述参与环境，不能替代趋势、估值、流动性和仓位管理。</p></section>
        </div>`
    },
    'index-constituents.html': {
      title: '股指成分监控使用说明',
      subtitle: '把指数涨跌拆成权重贡献、内部广度和期现证据',
      html: `
        <div class="page-guide-flow"><span><b>01</b>选 IH/IF/IC/IM</span><span><b>02</b>看复制质量</span><span><b>03</b>看权重广度</span><span><b>04</b>看期现是否确认</span><span><b>05</b>下钻驱动股</span></div>
        <div class="page-guide-grid">
          <section><h4>指数与权重</h4><p>IH、IF、IC、IM 分别对应上证50、沪深300、中证500和中证1000。优先使用中证指数官方收盘权重，并按权重日至当前的价格变化漂移；覆盖不足时回退自由流通分层代理，页面会披露来源和覆盖率。</p></section>
          <section><h4>贡献如何计算</h4><p class="page-guide-formula">个股贡献 bp = 昨收权重% × 个股涨跌幅%</p><p class="page-guide-formula">贡献点数 = 指数昨收 × 贡献 bp ÷ 10,000</p><p>复制收益为所有可计算贡献 bp 之和÷100；复制残差是实际指数收益减复制收益。残差或覆盖异常时，不应解读细分贡献。</p></section>
          <section><h4>内部趋势证据</h4><ul><li><b>上涨权重/下跌权重：</b>按指数权重计算的广度，比股票家数更接近期货标的。</li><li><b>站上开盘/VWAP 权重：</b>判断盘中买盘是否持续，而非仅由开盘跳空贡献。</li><li><b>贡献集中度：</b>Top 驱动股绝对贡献占比高，说明指数脆弱地依赖少数权重。</li><li><b>行业贡献：</b>同一行业内个股贡献 bp 求和。</li></ul></section>
          <section><h4>期货与基差</h4><p class="page-guide-formula">基差 = 期货标记价格 − 现货指数；基差率 = 基差 ÷ 现货 × 100%</p><p>年化基差按剩余期限折算；若利率、分红数据可用，会显示相对理论公允基差的残差。原始升贴水不自动产生多空方向信号，应结合 1/5/15 分钟现货贡献与期货变化。</p></section>
          <section><h4>树图和明细</h4><p>树图面积可切换指数权重、绝对贡献或流通市值；颜色表达涨跌或贡献方向。明细默认按绝对贡献排序，可按行业、搜索、估值和近期涨跌筛选。</p></section>
          <section><h4>股指期货实战</h4><p>趋势可信度最高的组合是：指数方向、贡献广度、VWAP 权重和期货方向一致且贡献不过度集中。若指数涨、期货弱、上涨权重下降或仅两三只权重股贡献，应视为背离。</p><p class="page-guide-warning">权重日期陈旧、实时报价覆盖不足或复制残差过大时，页面会降级；此时不要用成分贡献推断期货方向。</p></section>
        </div>`
    },
    'crowding.html': {
      title: '交易拥挤度使用说明',
      subtitle: '把注意力集中、退出脆弱和已经发生的去拥挤分开',
      html: `
        <div class="page-guide-flow"><span><b>01</b>看数据覆盖</span><span><b>02</b>识别集中</span><span><b>03</b>寻找独立脆弱证据</span><span><b>04</b>区分风险与已破坏</span></div>
        <div class="page-guide-grid">
          <section><h4>集中指标</h4><p>CR5/CR10 是成交额最大的 5/10 个行业或股票占比；Top10/Top50 是市场头部成交占比。</p><p class="page-guide-formula">HHI = Σ成交份额²；有效参与数 = 1 ÷ HHI</p><p>页面常把份额按百分数表达，因此有效数也可能显示为 <span class="page-guide-code">10,000/HHI</span>。CR 与 HHI 描述注意力分布，不是持仓。</p></section>
          <section><h4>历史标准化</h4><p>成交额、行业份额、内部 Top5、价格延伸和 Amihud 冲击等指标都与此前滚动历史比较。z-score 使用当前值相对过去窗口的均值和标准差；因果分位不包含未来日期。</p></section>
          <section><h4>矩阵放大操作</h4><p>申万三级点位密集时，先点“放大查看”，再用滚轮或 ＋/− 以光标位置为中心缩放；放大后拖动画布平移，点“复位”返回全景。气泡的可点击范围略大于可见圆点，便于选择小行业。</p></section>
          <section><h4>拥挤分</h4><p>市场层大致由集中分位45%、成交总额15%、5日龙头重叠15%、同向性10%、价格延伸10%、冲击成本5%加权。行业层由行业份额30%、成交额20%、内部Top5 15%、龙头重叠10%、价格延伸10%、同向性10%、冲击成本5%加权。</p><p>分数高只代表集中，需要第二类证据才能升级危险。</p></section>
          <section><h4>独立危险证据</h4><ul><li>内部涨跌宽度恶化或方向成交同步转弱</li><li>单位成交额价格冲击升高，退出承载下降</li><li>融资、ETF份额、机构持仓等直接集中证据</li><li>价格破坏、下行跳跃或成交努力失效</li></ul><p>外部数据缺失或过期保持“未知”，绝不按零风险处理。</p></section>
          <section><h4>状态解释</h4><p>集中观察＝注意力集中但未证实脆弱；脆弱警告＝至少两类相互独立证据；危险/去拥挤＝价格和流动性破坏已经发生。低分也可能只是数据覆盖不足，需先看质量。</p></section>
          <section><h4>建议用法与边界</h4><p>趋势早期成交集中可能是价格发现；后期高延伸、窄广度、龙头重叠和冲击成本共振才危险。结合热力图看扩散、成交动能看效率。</p><p class="page-guide-warning">拥挤不等于马上反转，去拥挤也不等于可以立刻抄底；该页用于仓位、追涨和退出风险管理。</p></section>
        </div>`
    },
    'capital-flow.html': {
      title: '成交动能使用说明',
      subtitle: '用相对参与、价格结果和内部广度判断量价是否真正共振',
      html: `
        <div class="page-guide-flow"><span><b>01</b>看量价象限</span><span><b>02</b>放大密集区域</span><span><b>03</b>看有效参与</span><span><b>04</b>看加速度与持续性</span><span><b>05</b>排除衰竭风险</span></div>
        <div class="page-guide-grid">
          <section><h4>量价罗盘</h4><p>横轴是行业相对市场收益除以自身历史波动后的价格强度分位；纵轴是行业成交份额相对过去 60 日中位数的活跃分位。气泡面积代表行业成交占比，颜色只表示当日涨跌。</p><p>右上是放量上涨确认，左上是放量下跌，右下是缩量上涨，左下是缩量下跌；象限是描述，不是固定买卖信号。</p></section>
          <section><h4>罗盘放大与点选</h4><p>申万三级点位密集时，可先点“放大查看”扩大画布；滚轮或 ＋/− 会以光标位置为中心连续缩放，放大后拖动画布平移，点“复位”回到全景。</p><p>需要直接检查一簇气泡时，点“框选”后在图中拖出矩形，系统会自动放大该区域。可见圆点之外还有更大的透明点击范围，小气泡绘制在大气泡上层，因此更容易点中。Esc 可退出框选或放大视图。</p></section>
          <section><h4>成交与方向代理</h4><p class="page-guide-formula">相对参与 = 当日行业成交份额 ÷ 此前60日成交份额中位数</p><p class="page-guide-formula">方向压力 = (上涨股成交额 − 下跌股成交额) ÷ 行业总成交额</p><p>方向压力来自日线涨跌与成交额，无法识别真实主动买卖方，不是主力净流入。</p></section>
          <section><h4>内部扩散</h4><p>活跃股票需先满足个股相对成交异常，再按行业当日方向选取同向股票。活跃广度＝同向活跃股票数÷有效股票数。</p><p class="page-guide-formula">有效参与者 N_eff = (Σ同向异常成交权重)² ÷ Σ权重²</p><p>N_eff 明显低于普通参与家数时，动能主要由少数龙头贡献。</p></section>
          <section><h4>动能、加速度和效率</h4><p>价格强度截断在 ±3 后乘以相对参与平方根形成动能；加速度＝当前动能−此前 3 日指数加权均值。持续性看最近 5 日方向压力与价格方向一致的比例。</p><p class="page-guide-formula">效率差 = 价格响应分位 − 成交活跃分位</p><p>≤−30 为高努力低响应，≥30 为低努力大位移，中间为量价匹配。</p></section>
          <section><h4>风险外圈和详情</h4><p>价格延伸、内部Top5集中、活跃广度下降、方向脉冲、放量杀跌和流动性真空独立判定。点击行业查看20日轨迹、Top5证据载体和风险理由；外圈颜色表达风险级别，不改变象限坐标。</p></section>
          <section><h4>建议用法与边界</h4><p>优先寻找“价格结果转强＋成交参与上升＋有效参与扩大＋风险不高”的行业；放量但价格无响应、有效参与下降时应减分。</p><p class="page-guide-warning">高成交额本身既不是机会也不是危险。不要把方向压力写成资金流入；数据覆盖不足50%时内部广度会保持缺失。</p></section>
        </div>`
    },
    'margin-financing.html': {
      title: '板块融资融券使用说明',
      subtitle: '用交易所逐证券明细观察行业杠杆余额、买入活跃度和变化方向',
      html: `
        <div class="page-guide-flow"><span><b>01</b>确认共同完整日</span><span><b>02</b>比较余额变化</span><span><b>03</b>用买入强度标准化</span><span><b>04</b>下钻个股</span></div>
        <div class="page-guide-grid">
          <section><h4>数据来源与日期</h4><p>逐证券融资融券明细来自上海证券交易所和深圳证券交易所。两所可能在不同时间发布最新数据，页面只展示两所均已发布的共同完整交易日，并分别披露各自最新日期。</p></section>
          <section><h4>融资指标</h4><p>融资余额是交易日结束后的存量；融资买入额是当日新增融资买入成交。余额变化＝本日融资余额−前日融资余额，其中还包含融资偿还、强平及权益调整，不能直接称为净买入。</p><p class="page-guide-formula">融资买入强度 = 当日融资买入额 ÷ 对应两融标的成交额</p></section>
          <section><h4>融券与两融余额</h4><p>深市融券余额直接采用交易所披露金额；沪市明细只披露融券余量，因此用当日收盘价×融券余量估算。两融余额＝融资余额＋融券余额。</p></section>
          <section><h4>三种行业分类</h4><p>同一份交易所原始明细分别按申万一级、同花顺和申万三级聚合；缺失分类归入“其他”。行业切换不会混用排名或历史数组。</p></section>
          <section><h4>如何比较</h4><p>绝对融资余额适合看杠杆集中在哪里，但会受行业规模影响；日/5日变化适合看存量方向，买入强度用于比较不同规模行业的当日融资参与。三者一致时证据更完整。</p></section>
          <section><h4>个股下钻与边界</h4><p>点击行业查看个股融资余额、余额变化、融资买入、买入强度、融券余额和两融余额，代码与名称可打开同花顺。</p><p class="page-guide-warning">融资增加既可能是趋势确认，也可能是杠杆追涨；融资下降既可能是主动降杠杆，也可能是偿还或强平。必须结合价格、成交动能、拥挤风险和行业扩散判断，不构成买卖建议。</p></section>
        </div>`
    },
    'market-cap.html': {
      title: '市值结构与行情归因使用说明',
      subtitle: '用市值权重、内部广度和贡献拆解行情由谁推动',
      html: `
        <div class="page-guide-flow"><span><b>01</b>看全市场贡献</span><span><b>02</b>比较 CW 与 EW</span><span><b>03</b>看规模集中</span><span><b>04</b>用树图和轨迹下钻</span></div>
        <div class="page-guide-grid">
          <section><h4>市值和收益口径</h4><p>总市值优先使用价格×时点总股本；缺少历史股本时使用明确标记的当前股本价格代理。A股流通市值代理来自本地行情字段，不等于指数公司的自由流通市值。</p><p class="page-guide-formula">行业市值 = Σ(股票价格 × 对应股本口径)</p></section>
          <section><h4>CW、EW 与广度</h4><p>市值加权收益 CW＝Σ期初市值权重×股票收益；等权收益 EW＝股票收益算术均值；股票广度＝上涨股票数÷有效股票数。</p><p>CW&gt;0、EW&lt;0 通常是权重托举；CW&lt;0、EW&gt;0 是少数大市值拖累；两者同向且广度高，行情更均衡。</p></section>
          <section><h4>罗盘放大操作</h4><p>申万三级气泡密集时，先点“放大查看”，再用滚轮或 ＋/− 以光标位置为中心缩放；放大后拖动画布平移，点“复位”回到完整收益区间。气泡的实际点击范围略大于可见圆点，小行业会绘制在大气泡上层。</p></section>
          <section><h4>贡献和权重迁移</h4><p class="page-guide-formula">行业贡献 bp ≈ 行业期初市场权重% × 行业 CW 收益%</p><p class="page-guide-formula">权重迁移 bp = (当前行业权重% − 基期行业权重%) × 100</p><p>贡献 bp 是市场收益归因，不是资金流入；权重上升可能来自价格上涨、股本变化或样本进出。</p></section>
          <section><h4>集中结构</h4><p>行业 HHI＝Σ行业市值权重²，有效行业数＝10,000÷HHI；Top3/Top5 是头部行业市值占比。行业内部 Top5 和有效股票数用相同思想衡量是否由少数股票支撑。</p><p>大盘 Top100、中盘 Next400、其余股票分层用于识别大小盘风格迁移。</p></section>
          <section><h4>树图和窗口</h4><p>树图面积代表市值，颜色按固定的窗口收益阈值映射，因此跨日期可直接比较。1/5/20 日切换改变收益、贡献和基期，不改变行业分类。点击行业查看 20–60 日市值、权重、贡献和成分驱动。</p></section>
          <section><h4>Price / Supply / Universe</h4><p>Price 是价格效应；Supply 只有存在可靠时点股本时才计算；Universe 表示样本进入退出。停牌价格承接、历史行业按当前分类回溯、时点股本覆盖率都会单独披露。</p><p class="page-guide-warning">市值增加不是等额资金净流入。股本或样本数据不足时不要解释 Supply/Universe，页面不会伪造拆分。</p></section>
        </div>`
    },
    'etf-recommend.html': {
      title: 'ETF 下一热点使用说明',
      subtitle: '先寻找行业扩散，再验证 ETF 需求、相对强度、可交易性和风险',
      html: `
        <div class="page-guide-flow"><span><b>01</b>确认市场允许</span><span><b>02</b>看 ETF 去重候选</span><span><b>03</b>核对四类证据</span><span><b>04</b>检查风险与失效</span></div>
        <div class="page-guide-grid">
          <section><h4>预测单位和映射</h4><p>榜单以唯一可交易 ETF 为单位，同一 ETF 关联的多个申万三级行业会聚合，避免重复占榜。映射可信度权重：人工覆盖/申万三级 1.00、二级 0.65、一级 0.35；低层级只作较弱解释。</p></section>
          <section><h4>机会证据</h4><p>行业扩散来自新高宽度、扩散加速度和净宽度；需求优先使用交易所 ETF 份额变化，缺失时才以方向成交代理作为弱证据；相对强度比较 ETF 与沪深300 ETF；可交易性结合20日成交额和波动。</p><p class="page-guide-formula">原始机会 = 市场环境15% + 需求25% + 扩散25% + 相对强度25% + 可交易性10%</p></section>
          <section><h4>风险如何扣分</h4><p>风险分由行业拥挤45%、价格延伸30%、流动性15%、市场环境10%构成。仅当风险高于38时按 <span class="page-guide-code">(风险−38)×0.42</span> 扣减；5/20日涨幅过热、偏离MA20、份额下降和高波动会列出具体原因。</p></section>
          <section><h4>阶段含义</h4><ul><li><b>确认：</b>机会≥66，扩散、需求和相对强度同时达标。</li><li><b>萌芽：</b>达到最低分，扩散改善且需求/相对强度至少一项确认。</li><li><b>观察：</b>证据不够完整。</li><li><b>回避：</b>风险过高或市场限制承担风险。</li><li><b>数据不足：</b>质量低或缺少有效行业映射。</li></ul><p>榜单允许为空，不强行填满。</p></section>
          <section><h4>概率与质量</h4><p>机会分是横截面证据排序；数据质量分衡量行情特征、映射、基准、市场状态、拥挤和份额数据的完整度。概率只有在滚动样本外校准达到独立日期与样本门槛后显示。</p></section>
          <section><h4>建议用法与失效</h4><p>优先看刚由观察转萌芽、行业扩散上升、ETF相对宽基转强且份额未流出的标的；已过热的高分 ETF 不是“下一热点”。</p><p class="page-guide-warning">ETF份额增加是申赎证据但不保证价格上涨；方向成交代理不是真实净流入。以页面给出的失效条件和自身仓位约束为准。</p></section>
        </div>`
    },
    'momentum-etf.html': {
      title: '动量 ETF 使用说明',
      subtitle: '与参考页同一套排名引擎：历史行情直接回算，不等待逐日累计',
      html: `
        <div class="page-guide-flow"><span><b>01</b>选分组</span><span><b>02</b>设置参数</span><span><b>03</b>看排名轨迹</span><span><b>04</b>核对未入选原因</span></div>
        <div class="page-guide-grid">
          <section><h4>动量如何计算</h4><p>对回看窗口内的对数收盘价做加权线性回归，权重从1线性增加到2，越近的数据权重越高；斜率按250个交易日年化。</p><p class="page-guide-formula">年化趋势 = exp(回归斜率 × 250) − 1；动量得分 = 年化趋势 × R²</p><p>默认回看25日，并同时计算10日短期年化动量。</p></section>
          <section><h4>过滤链</h4><ul><li>先检查盈利保护：最新价相对指定窗口最高价的回撤不得超过阈值。</li><li>启用时，短期年化动量必须达到下限。</li><li>近3日任一单日跌幅不得超过限制。</li><li>分数必须严格位于最小值与最大值之间。</li></ul></section>
          <section><h4>池子与分组</h4><p>默认选择“五福组”，也可切换“行业主题全量组”或全部标的。ETF 池和自定义分组保存在本机；恢复默认池会同时恢复参考文件的默认标的及分组。</p></section>
          <section><h4>排名轨迹</h4><p>页面直接抓取东方财富前复权日线，失败时改用腾讯财经；随后针对每个历史交易日仅使用该日及以前的数据重新计算，因此无需等待排名逐日累计。</p><p>轨迹包含所选日期内所有曾进入 Top N 的 ETF；断档不连线，中途离榜标注“退出”，相对前一日新进榜单标注 NEW。</p></section>
          <section><h4>参数管理</h4><p>13项参数与参考文件一致：长短动量窗口、短期过滤、三日跌幅、盈利保护、分数上下限、Top N、排序方向和回溯交易日数。改参数后点“重新计算”；只有所需历史长度超出现有缓存时才重新联网。</p></section>
          <section><h4>排名与未入选</h4><p>日期标签可切换当日榜单；变化值与前一个交易日的完整入选榜比较。5/10/20日列是对应区间实际收益。未入选明细按当日无行情、数据不足、盈利保护、短期动量、近3日跌幅或分数范围归类。</p></section>
          <section><h4>边界</h4><p class="page-guide-warning">年化数只是历史回归斜率换算，不是未来收益预测。名次变化是所选分组、参数和行情数据下的相对结果，不构成买卖建议。</p></section>
        </div>`
    },
    'etf-backtest.html': {
      title: 'ETF 回测使用说明',
      subtitle: '检查模型在严格时间顺序下是否优于基线，以及优势是否稳定',
      html: `
        <div class="page-guide-flow"><span><b>01</b>确认样本区间</span><span><b>02</b>看可评估日期</span><span><b>03</b>看 T+1—T+5 路径</span><span><b>04</b>看横截面指标</span><span><b>05</b>看分期稳定性</span></div>
        <div class="page-guide-grid">
          <section><h4>时间规则</h4><p>每个推荐日 D 只使用 D 日收盘前可获得的数据，统一在下一市场交易日 T+1 开盘成交，再依次观察 T+1、T+2、T+3、T+4、T+5 收盘。它是一笔交易的五个观察点，不是五次重复入场。若 ETF 在严格 T+1 没有开盘行情，则该笔不可评估，不把之后首个交易日冒充 T+1。</p></section>
          <section><h4>收益口径</h4><p class="page-guide-formula">T+n净收益 = T+n收盘 ÷ T+1开盘 − 1 − 交易成本，n∈[1,5]</p><p>页面只保留 T+1 至 T+5。基准使用沪深300 ETF 同期 T+1开盘至对应目标日收盘且不扣成本；净超额＝ETF净收益−基准收益。连续五个期限可以判断机会是快速兑现、持续增强，还是很快衰减。</p></section>
          <section><h4>命中和横截面</h4><p>热点命中通常要求净收益&gt;0、超额收益&gt;0，且同日横截面收益分位≥80%。Precision@K 是每日 TopK 中命中比例的日期均值；Rank IC 是当日预测分数与未来收益排序的相关系数。</p></section>
          <section><h4>基线和公平比较</h4><p>模型与简单相对强弱基线使用同一推荐日、同一可交易 ETF 宇宙、同一 T+1 入场和未来标签。只比较模型榜单内标的会产生选择偏差，因此结果宇宙包含所有已配置且严格 T+1 可交易的 ETF。</p></section>
          <section><h4>概率校准</h4><p>热点概率统一以 T+5 标签校准，训练期和验证期按时间拆分，并避免训练期未来标签跨入验证期。T+1 至 T+4 只展示兑现路径，不各自拟合小样本概率。只有独立预测日期和样本数足够、校准分箱具有稳定性时，预测概率才可展示；否则标记“校准中”。</p></section>
          <section><h4>如何判断有效</h4><p>先看超额从 T+1 到 T+5 何时出现、能否保持，再结合 T+5 的 Precision@K、Rank IC、不同年份/市场状态及预测日聚类后的置信区间。若优势只在个别日期或某一个观察点短暂出现，通常不具稳健性。</p><p class="page-guide-warning">回测不是实盘承诺。日线无法无穿越复原14:50盘中信号，只有真实保存分钟级特征快照后才能回测盘中策略。</p></section>
        </div>`
    }
  };

  function pageName() {
    var name = (window.location.pathname.split('/').pop() || '').toLowerCase();
    return name === 'industry-heatmap-standalone.html' ? 'industry-heatmap.html' : name;
  }

  function addStyle() {
    if (document.getElementById('pageGuideStyles')) return;
    var style = document.createElement('style');
    style.id = 'pageGuideStyles';
    style.textContent = `
      .page-guide-shell{width:calc(100% - 32px);max-width:1560px;margin:18px auto 24px;color:var(--text,#abb2bf);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;box-sizing:border-box}
      .page-guide-details{border:1px solid var(--border,#343a46);border-radius:9px;background:var(--panel,#1f2329);overflow:hidden;box-shadow:0 4px 18px rgba(0,0,0,.12)}
      .page-guide-details>summary{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 16px;cursor:pointer;list-style:none;color:var(--strong,#e5e5e5);font-size:14px;font-weight:700;user-select:none}
      .page-guide-details>summary::-webkit-details-marker{display:none}
      .page-guide-details>summary::after{content:"展开 +";color:var(--accent,#61afef);font-size:11px;font-weight:600}
      .page-guide-details[open]>summary{border-bottom:1px solid var(--border,#343a46)}
      .page-guide-details[open]>summary::after{content:"收起 −"}
      .page-guide-summary-note{margin-left:auto;color:var(--muted,#7f8792);font-size:11px;font-weight:400}
      .page-guide-content{padding:15px 16px 17px}
      .page-guide-flow{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:13px}
      .page-guide-flow span{padding:6px 9px;border:1px solid var(--border,#343a46);border-radius:999px;background:var(--panel-2,#252a32);color:var(--muted,#9ba5b4);font-size:11px}
      .page-guide-flow b{margin-right:5px;color:var(--accent,#61afef)}
      .page-guide-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
      .page-guide-grid section{padding:11px 12px;border:1px solid var(--border,#303641);border-radius:7px;background:var(--panel-2,#20242b)}
      .page-guide-grid h4{margin:0 0 7px;color:var(--strong,#d7dce2);font-size:12px}
      .page-guide-grid p,.page-guide-grid li{font-size:11px;line-height:1.72}
      .page-guide-grid p{margin:5px 0}
      .page-guide-grid ul,.page-guide-grid ol{margin:5px 0;padding-left:18px}
      .page-guide-grid b{color:var(--text,#c7cdd5)}
      .page-guide-formula{padding:6px 8px;border-left:3px solid var(--accent,#61afef);background:var(--bg,#1b1f25);color:var(--text,#c8d9e8);font-family:"SF Mono",Menlo,Consolas,monospace}
      .page-guide-code{color:#e5c07b;font-family:"SF Mono",Menlo,Consolas,monospace}
      .page-guide-warning{padding:7px 9px;border:1px solid #e5c07b55;border-radius:5px;background:#e5c07b0d;color:#d8c28b}
      @media(max-width:860px){.page-guide-grid{grid-template-columns:1fr}.page-guide-shell{width:calc(100% - 20px)}.page-guide-summary-note{display:none}}
    `;
    document.head.appendChild(style);
  }

  function render() {
    var guide = guides[pageName()];
    if (!guide || document.getElementById('pageUsageGuide')) return;
    addStyle();
    var shell = document.createElement('section');
    shell.className = 'page-guide-shell';
    shell.id = 'pageUsageGuide';
    shell.setAttribute('aria-label', guide.title);
    shell.innerHTML =
      '<details class="page-guide-details">' +
        '<summary><span>📖 ' + guide.title + '</span>' +
          '<span class="page-guide-summary-note">' + guide.subtitle + '</span></summary>' +
        '<div class="page-guide-content">' + guide.html + '</div>' +
      '</details>';
    var guideHost = pageName() === 'industry-heatmap.html'
      ? document.querySelector('.container')
      : document.body;
    (guideHost || document.body).appendChild(shell);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();
