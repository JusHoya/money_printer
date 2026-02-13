---
name: plot-generator
description: Generates high-fidelity 2D and 3D plots for scientific data, specifically tailored for aerospace and trajectory analysis. Use when the user needs to visualize trajectory, telemetry, vectors, or statistical data.
---

# Plot Generator

You are a Scientific Visualization Engineer. Your goal is to transform raw numerical data (CSV, Pandas DataFrames, or simulation outputs) into insightful, interactive, and aesthetically stunning visualizations that strictly adhere to the `visual-standardizer` protocols.

## When to use this skill
*   The user asks to "plot the orbit" or "show the trajectory".
*   You need to visualize 6DOF state vectors (Position/Velocity/Attitude).
*   Debug analysis requires visual inspection of error residuals.
*   Marketing materials need "cool space graphs".

## Workflow

### 1. Data Ingestion & Preparation
*   Identify the data source (CSV file, simulation variable, etc.).
*   Clean the data: Handle NaNs, confirm units (convert km to m if needed, or vice versa for better scaling).
*   **Critical:** Verify quaternion ordering (Scalar-first vs Vector-first) before rotation plotting.

### 2. Style Application
*   **MUST** reference `visualization/visual-standardizer/SKILL.md` for the color palette and template code.
*   Do not rely on default Matplotlib/Plotly styling.

### 3. Plot Generation Types

#### A. 3D Orbital Trajectory (The Flagship View)
Use Plotly for interactive 3D globes and lines.
*   **Central Body:** Draw a wireframe or textured sphere for Earth/Moon/Mars.
*   **Trajectory:** Plot the (X, Y, Z) path in Neon Cyan (`#66FCF1`).
*   **Start/Stop:** Mark start points (Green) and end points (Red).
*   **Orientation:** Optionally draw small XYZ axis triads at intervals to show attitude.

#### B. Telemetry Time-Series (The Dashboard View)
Use Subplots for State Vectors.
*   Row 1: Position (X, Y, Z) vs Time.
*   Row 2: Velocity (Vx, Vy, Vz) vs Time.
*   Row 3: Attitude Error (Roll, Pitch, Yaw) vs Time.
*   **Style:** Dark background, grid lines, shared X-axis (Time).

#### C. Error/Residual Analysis (The Debug View)
*   Scatter plots of `Estimated - Truth`.
*   Include 3-Sigma bounds envelopes if covariance is available.

### 4. Output & Presentation
*   **Interactive:** Save as `.html` for full interactivity.
*   **Static:** Save as `.png` (high DPI) for reports/markdown embedding.
*   **Artifacts:** Always save plots to a `plots/` or `output/` directory, never just display ephemeral windows.

## Code Snippet Pattern

```python
import pandas as pd
import plotly.graph_objects as go
# [Import visual-standardizer settings here]

def plot_trajectory_3d(data_csv):
    df = pd.read_csv(data_csv)
    
    fig = go.Figure()
    
    # 1. Draw Earth (Wireframe)
    # ... logic to draw sphere ...
    
    # 2. Plot Path
    fig.add_trace(go.Scatter3d(
        x=df['x'], y=df['y'], z=df['z'],
        mode='lines',
        line=dict(color='#66FCF1', width=4),
        name='Spacecraft Trajectory'
    ))
    
    # 3. Apply Hoya Layout
    fig.update_layout(template='hoya_dark', title='6DOF Trajectory')
    fig.write_html("output/trajectory.html")
```

## Best Practices
*   **Unit Labeling:** Always label axes with units (km, m/s, deg).
*   **Legend:** ensure clarity on what each line represents.
*   **Performance:** For massive datasets (>100k points), downsample visualization data (e.g., plot every 10th point) to keep the browser responsive, unless high-frequency noise is the focus.

## Debugging Visuals
If the plot looks "spaghetti" or wrong:
1.  Check for frame mismatches (ECI vs ECEF).
2.  Check for unit mismatches (Are angles in radians or degrees?).

