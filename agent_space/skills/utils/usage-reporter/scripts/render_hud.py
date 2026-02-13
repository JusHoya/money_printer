import sys

def render_hud(current_tokens, max_tokens=1000000):
    # Constants
    COST_PER_1M_INPUT = 0.10
    COST_PER_1M_OUTPUT = 0.40 # Blended average
    
    # Calculations
    percentage = (current_tokens / max_tokens) * 100
    est_cost = current_tokens * 1e-7 # Simplified $0.10/1M
    
    # Formatting
    width = 64
    bar_width = 48 # Slightly smaller to ensure fit
    
    # Bar Logic
    if percentage > 100: percentage = 100
    filled_len = int(bar_width * (percentage / 100))
    # Ensure at least 0 if 0
    bar_str = "█" * filled_len + "░" * (bar_width - filled_len)
    
    # Strings
    title = "G E M I N I   U S A G E"
    
    # Row Content construction (Content only, no borders)
    # We want 2 spaces padding on left usually
    row_context = f"  CONTEXT WINDOW: {max_tokens:,} Tokens"
    
    usage_str = f"~{current_tokens:,} Tokens ({percentage:.2f}%)"
    row_usage = f"  USED:             {usage_str}"
    
    row_bar = f"  [{bar_str}] {int(percentage)}%"
    
    cost_str = f"${est_cost:.4f}"
    row_cost = f"  EST. COST:      {cost_str}"

    # Print Helper
    def print_row(content, border_char="║"):
        # Pad content to exactly 'width'
        print(f"  {border_char}{content:<{width}}{border_char}")

    # Render
    print(f"  ╔{'═' * width}╗")
    print_row(f"{title:^{width}}")
    print(f"  ╠{'═' * width}╣")
    print_row(row_context)
    print_row(row_usage)
    print_row(row_bar)
    print(f"  ╠{'═' * width}╣")
    print_row(row_cost)
    print(f"  ╚{'═' * width}╝")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        render_hud(8250)
    else:
        try:
            tokens = int(sys.argv[1].replace(',', ''))
            render_hud(tokens)
        except ValueError:
            print("Invalid token count provided.")