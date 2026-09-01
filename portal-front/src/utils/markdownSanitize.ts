import type { Options as ReactMarkdownOptions } from 'react-markdown'
import rehypeSanitize, { defaultSchema, type Options as SanitizeSchema } from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'

export const portalMarkdownRemarkPlugins: NonNullable<ReactMarkdownOptions['remarkPlugins']> = [
  [remarkGfm, { singleTilde: false }],
]

export const portalMarkdownSchema = {
  ...defaultSchema,
  tagNames: [
    'a',
    'blockquote',
    'br',
    'code',
    'del',
    'em',
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',
    'hr',
    'input',
    'li',
    'ol',
    'p',
    'pre',
    'strong',
    'table',
    'tbody',
    'td',
    'th',
    'thead',
    'tr',
    'ul',
  ],
  attributes: {
    a: ['href', 'title'],
    code: [['className', /^language-[\w-]+$/]],
    input: [['type', 'checkbox'], 'checked', 'disabled'],
    li: [['className', 'task-list-item']],
    ol: ['start'],
    td: ['align', 'colSpan', 'rowSpan'],
    th: ['align', 'colSpan', 'rowSpan'],
    ul: [['className', 'contains-task-list']],
  },
  protocols: {
    href: ['http', 'https', 'mailto'],
  },
  strip: ['iframe', 'script', 'style'],
} satisfies SanitizeSchema

export const portalMarkdownRehypePlugins: NonNullable<ReactMarkdownOptions['rehypePlugins']> = [
  [rehypeSanitize, portalMarkdownSchema],
]
