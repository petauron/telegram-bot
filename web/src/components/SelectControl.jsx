import { Children, isValidElement } from 'react'
import * as Select from '@radix-ui/react-select'
import { Check, ChevronDown } from 'lucide-react'

// Encode all values so the empty string remains a selectable option.
const encode = (value) => `option:${String(value ?? '')}`

export function SelectControl({ children, value, onChange, disabled, ...props }) {
  const options = Children.toArray(children).filter(isValidElement)
  const selected = options.find((option) => encode(option.props.value) === encode(value))
  return (
    <Select.Root value={encode(value)} disabled={disabled} onValueChange={(next) => onChange?.({ target: { value: next.slice(7), type: 'select-one' } })}>
      <Select.Trigger className="app-select-trigger" {...props}>
        <Select.Value>{selected?.props.children}</Select.Value>
        <Select.Icon asChild><ChevronDown size={14} aria-hidden="true" /></Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Content className="app-select-content" position="popper" sideOffset={4}>
          <Select.Viewport>
            {options.map((option) => (
              <Select.Item className="app-select-item" key={encode(option.props.value)} value={encode(option.props.value)} disabled={option.props.disabled}>
                <Select.ItemText>{option.props.children}</Select.ItemText>
                <Select.ItemIndicator className="app-select-indicator"><Check size={14} aria-hidden="true" /></Select.ItemIndicator>
              </Select.Item>
            ))}
          </Select.Viewport>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  )
}
