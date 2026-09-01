import {
  ArcElement,
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Legend,
  LineElement,
  LinearScale,
  PointElement,
  Tooltip,
  type ChartData,
  type ChartOptions,
} from 'chart.js'
import { Bar, Doughnut, Line } from 'react-chartjs-2'

import type { MarketChart, MarketChartSeries } from '../../utils/marketStream'

ChartJS.register(
  ArcElement,
  BarElement,
  CategoryScale,
  Legend,
  LineElement,
  LinearScale,
  PointElement,
  Tooltip,
)

interface Props {
  charts: MarketChart[]
  error?: string
}

const SERIES_COLORS = ['#087EA4', '#157347', '#B54708', '#7A5AF8', '#B42318'] as const

function dataset(series: MarketChartSeries, index: number) {
  const color = SERIES_COLORS[index % SERIES_COLORS.length]
  return {
    label: series.label,
    data: series.values,
    borderColor: color,
    backgroundColor: color,
    spanGaps: false,
  }
}

function cartesianOptions(chart: MarketChart): ChartOptions<'line' | 'bar'> {
  const unit = chart.unit
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: {
      legend: { position: 'bottom' },
      tooltip: {
        callbacks: {
          label: context => `${context.dataset.label}: ${context.formattedValue}${unit ?? ''}`,
        },
      },
    },
    scales: {
      x: {
        title: chart.x_label ? { display: true, text: chart.x_label } : undefined,
      },
      y: {
        beginAtZero: false,
        title: unit ? { display: true, text: unit } : undefined,
      },
    },
  }
}

function MarketChartView({ chart }: { chart: MarketChart }) {
  if (chart.chart_type === 'doughnut') {
    const data = {
      labels: chart.x,
      datasets: chart.series.map(dataset),
    } as unknown as ChartData<'doughnut'>
    return <Doughnut data={data} options={{ responsive: true, maintainAspectRatio: false, animation: false }} />
  }
  const data = {
    labels: chart.x,
    datasets: chart.series.map(dataset),
  }
  return chart.chart_type === 'bar'
    ? <Bar data={data} options={cartesianOptions(chart) as ChartOptions<'bar'>} />
    : <Line data={data} options={cartesianOptions(chart) as ChartOptions<'line'>} />
}

export default function MarketCharts({ charts, error }: Props) {
  if (charts.length === 0) {
    return error ? <div className="market-chart-error" role="alert">{error}</div> : null
  }
  return (
    <>
      {error && <div className="market-chart-error" role="alert">{error}</div>}
      <div className="market-charts" aria-label="시장 분석 차트">
        {charts.map(chart => (
          <section className="market-chart" key={chart.chart_id} data-chart-id={chart.chart_id}>
            <h3>{chart.title}</h3>
            <p className="market-chart-source">{chart.source_label}</p>
            <div className="market-chart-canvas">
              <MarketChartView chart={chart} />
            </div>
          </section>
        ))}
      </div>
    </>
  )
}
