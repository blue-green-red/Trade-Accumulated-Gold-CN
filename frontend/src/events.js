const EVENT_CN = {
  "Inflation Rate YoY": "CPI 年率",
  "Inflation Rate MoM": "CPI 月率",
  "Core Inflation Rate YoY": "核心 CPI 年率",
  "Core Inflation Rate MoM": "核心 CPI 月率",
  "PPI MoM": "PPI 月率",
  "PPI YoY": "PPI 年率",
  "Core PCE Price Index MoM": "核心 PCE 月率",
  "Core PCE Price Index YoY": "核心 PCE 年率",
  "Non Farm Payrolls": "非农就业人数",
  "Unemployment Rate": "失业率",
  "Fed Interest Rate Decision": "美联储利率决议",
  "Fed Press Conference": "美联储新闻发布会",
  "FOMC Economic Projections": "FOMC 经济预测",
  "FOMC Minutes": "FOMC 会议纪要",
  "Retail Sales MoM": "零售销售月率",
  "Retail Sales YoY": "社会消费品零售年率",
  "GDP Growth Rate YoY": "GDP 年率",
  "GDP Growth Rate QoQ Adv": "GDP 季率初值",
  "GDP Growth Rate QoQ Final": "GDP 季率终值",
  "Industrial Production YoY": "工业增加值年率",
  "Building Permits Prel": "营建许可初值",
  "Housing Starts": "新屋开工",
  "Existing Home Sales": "成屋销售",
  "Durable Goods Orders MoM": "耐用品订单月率",
  "ISM Manufacturing PMI": "ISM 制造业 PMI",
  "ISM Services PMI": "ISM 服务业 PMI",
  "JOLTs Job Openings": "JOLTs 职位空缺",
  "Michigan Consumer Sentiment Prel": "密歇根消费者信心初值",
  "Personal Income MoM": "个人收入月率",
  "Personal Spending MoM": "个人支出月率",
};
export const eventName = (t) =>
  EVENT_CN[t] ||
  (/^Fed Chair (.+) Speech$/.test(t)
    ? `美联储主席${t.replace(/^Fed Chair (.+) Speech$/, " $1 ")}讲话`
    : t);
