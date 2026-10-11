"""RRG visualization components."""

import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np
from matplotlib.figure import Figure
from typing import List, Optional

from src.rrg.models import RRGDataPoint, RRGQuadrant, RRGSnapshot


class RRGVisualizer:
    """Creates RRG (Relative Rotation Graph) visualizations."""

    # Quadrant colors (matching the reference chart)
    QUADRANT_COLORS = {
        RRGQuadrant.LEADING: "#90EE90",     # Light green
        RRGQuadrant.IMPROVING: "#87CEEB",   # Light blue
        RRGQuadrant.LAGGING: "#FFB6C1",     # Light red
        RRGQuadrant.WEAKENING: "#FFE4B5",   # Light yellow
    }

    QUADRANT_LABELS = {
        RRGQuadrant.LEADING: "LEADING",
        RRGQuadrant.IMPROVING: "IMPROVING",
        RRGQuadrant.LAGGING: "LAGGING",
        RRGQuadrant.WEAKENING: "WEAKENING",
    }

    def __init__(self, figsize: tuple = (12, 10)):
        self.figsize = figsize

    def create_rrg_chart(
        self,
        snapshot: RRGSnapshot,
        title: str = None,
        save_path: Optional[str] = None
    ) -> Figure:
        """
        Create RRG scatter plot.
        
        Args:
            snapshot: RRG snapshot with data points
            title: Chart title (auto-generated if None)
            save_path: Path to save the figure (optional)
            
        Returns:
            matplotlib Figure object
        """
        if title is None:
            title = f"{snapshot.benchmark_symbol} Options Strike Relative Rotation Graph (RRG)"

        fig, ax = plt.subplots(figsize=self.figsize)
        
        # Set up the plot with proper axis ranges
        self._setup_axes(ax)
        
        # Draw quadrant backgrounds
        self._draw_quadrant_backgrounds(ax)
        
        # Draw reference lines at 100
        self._draw_reference_lines(ax)
        
        # Plot data points by quadrant
        self._plot_data_points(ax, snapshot.data_points)
        
        # Add labels and styling
        self._add_labels(ax, snapshot)
        
        # Set title
        ax.set_title(title, fontsize=14, fontweight='bold', pad=20)
        
        # Adjust layout
        plt.tight_layout()
        
        # Save if path provided
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
        
        return fig

    def _setup_axes(self, ax):
        """Set up axis ranges and labels."""
        # Set axis ranges (centered around 100)
        ax.set_xlim(94, 106)
        ax.set_ylim(94, 106)
        
        # Set axis labels
        ax.set_xlabel('RS-Ratio (Trend Strength)', fontsize=12, fontweight='bold')
        ax.set_ylabel('RS-Momentum (Rate of Change)', fontsize=12, fontweight='bold')
        
        # Add grid
        ax.grid(True, alpha=0.3, linestyle='--')
        
        # Set equal aspect ratio
        ax.set_aspect('equal')

    def _draw_quadrant_backgrounds(self, ax):
        """Draw colored backgrounds for each quadrant."""
        # Define quadrant boundaries using fixed coordinates to match axis setup
        x_min, x_max = 94, 106
        y_min, y_max = 94, 106
        
        # Draw quadrant rectangles with proper coordinates
        quadrants = [
            (RRGQuadrant.LEADING, (100, x_max, 100, y_max)),      # Top-right
            (RRGQuadrant.IMPROVING, (x_min, 100, 100, y_max)),  # Top-left
            (RRGQuadrant.LAGGING, (x_min, 100, y_min, 100)),    # Bottom-left
            (RRGQuadrant.WEAKENING, (100, x_max, y_min, 100)),  # Bottom-right
        ]
        
        for quadrant, (x1, x2, y1, y2) in quadrants:
            rect = patches.Rectangle(
                (x1, y1), x2 - x1, y2 - y1,
                linewidth=0,
                edgecolor='none',
                facecolor=self.QUADRANT_COLORS[quadrant],
                alpha=0.3,
                zorder=0
            )
            ax.add_patch(rect)
            
            # Add quadrant label
            label_x = x1 + (x2 - x1) / 2
            label_y = y1 + (y2 - y1) / 2
            ax.text(
                label_x, label_y,
                self.QUADRANT_LABELS[quadrant],
                fontsize=11,
                fontweight='bold',
                ha='center',
                va='center',
                alpha=0.7,
                zorder=1
            )

    def _draw_reference_lines(self, ax):
        """Draw reference lines at RS-Ratio = 100 and RS-Momentum = 100."""
        # Vertical line at RS-Ratio = 100
        ax.axvline(x=100, color='black', linestyle='-', linewidth=1.5, alpha=0.5, zorder=2)
        
        # Horizontal line at RS-Momentum = 100
        ax.axhline(y=100, color='black', linestyle='-', linewidth=1.5, alpha=0.5, zorder=2)

    def _plot_data_points(self, ax, data_points: List[RRGDataPoint]):
        """Plot data points colored by quadrant."""
        # Group points by quadrant
        points_by_quadrant = {quadrant: [] for quadrant in RRGQuadrant}
        
        for point in data_points:
            points_by_quadrant[point.quadrant].append(point)
        
        # Plot each quadrant's points
        for quadrant, points in points_by_quadrant.items():
            if not points:
                continue
            
            # Extract coordinates
            x_coords = [point.rs_ratio for point in points]
            y_coords = [point.rs_momentum for point in points]
            
            # Get color for this quadrant
            color = self._get_quadrant_edge_color(quadrant)
            
            # Plot points
            ax.scatter(
                x_coords, y_coords,
                c=color,
                s=100,
                alpha=0.8,
                edgecolors='black',
                linewidth=1,
                zorder=3
            )
            
            # Add labels for points
            for point in points:
                label = f"{int(point.strike)} {point.option_type}"
                ax.annotate(
                    label,
                    (point.rs_ratio, point.rs_momentum),
                    xytext=(5, 5),
                    textcoords='offset points',
                    fontsize=8,
                    alpha=0.8,
                    zorder=4
                )

    def _add_labels(self, ax, snapshot: RRGSnapshot):
        """Add additional labels and metadata."""
        # Add timestamp
        time_text = f"Timestamp: {snapshot.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
        ax.text(
            0.02, 0.02,
            time_text,
            transform=ax.transAxes,
            fontsize=9,
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.8),
            zorder=5
        )
        
        # Add benchmark info
        benchmark_text = f"Benchmark: {snapshot.benchmark_symbol} @ {snapshot.benchmark_ltp:.2f}"
        ax.text(
            0.02, 0.95,
            benchmark_text,
            transform=ax.transAxes,
            fontsize=10,
            fontweight='bold',
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.8),
            zorder=5
        )
        
        # Add timeframe info
        timeframe_text = f"Timeframe: {snapshot.timeframe}"
        ax.text(
            0.98, 0.95,
            timeframe_text,
            transform=ax.transAxes,
            fontsize=9,
            ha='right',
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.8),
            zorder=5
        )
        
        # Add note
        note_text = "Strike positions relative to benchmark"
        ax.text(
            0.98, 0.02,
            note_text,
            transform=ax.transAxes,
            fontsize=8,
            ha='right',
            style='italic',
            alpha=0.7,
            zorder=5
        )

    def _get_quadrant_edge_color(self, quadrant: RRGQuadrant) -> str:
        """Get edge color for quadrant points."""
        colors = {
            RRGQuadrant.LEADING: "#228B22",      # Forest green
            RRGQuadrant.IMPROVING: "#4169E1",    # Royal blue
            RRGQuadrant.LAGGING: "#DC143C",      # Crimson
            RRGQuadrant.WEAKENING: "#DAA520",    # Goldenrod
        }
        return colors.get(quadrant, "black")
    
    def _determine_quadrant(self, rs_ratio: float, rs_momentum: float) -> RRGQuadrant:
        """Determine quadrant based on RS-Ratio and RS-Momentum."""
        if rs_ratio > 100 and rs_momentum > 100:
            return RRGQuadrant.LEADING
        elif rs_ratio < 100 and rs_momentum > 100:
            return RRGQuadrant.IMPROVING
        elif rs_ratio < 100 and rs_momentum < 100:
            return RRGQuadrant.LAGGING
        else:  # rs_ratio > 100 and rs_momentum < 100
            return RRGQuadrant.WEAKENING

    def create_quadrant_summary(self, snapshot: RRGSnapshot) -> dict:
        """Create summary statistics by quadrant."""
        summary = {quadrant.value: [] for quadrant in RRGQuadrant}
        
        for point in snapshot.data_points:
            summary[point.quadrant.value].append({
                "strike": point.strike,
                "option_type": point.option_type,
                "rs_ratio": point.rs_ratio,
                "rs_momentum": point.rs_momentum,
                "rs": point.rs,
            })
        
        # Add counts
        for quadrant in summary:
            summary[quadrant] = {
                "count": len(summary[quadrant]),
                "strikes": summary[quadrant]
            }
        
        return summary


def create_rrg_chart_from_data(
    rs_ratios: List[float],
    rs_momentums: List[float],
    labels: List[str],
    title: str = "RRG Chart"
) -> Figure:
    """
    Create RRG chart from raw data arrays.
    
    Args:
        rs_ratios: List of RS-Ratio values
        rs_momentums: List of RS-Momentum values
        labels: List of labels for each point
        title: Chart title
        
    Returns:
        matplotlib Figure object
    """
    visualizer = RRGVisualizer()
    
    # Create mock snapshot from raw data
    from src.rrg.models import RRGDataPoint, RRGQuadrant, RRGSnapshot
    from src.timeutil import now_ist
    
    data_points = []
    for rs_ratio, rs_momentum, label in zip(rs_ratios, rs_momentums, labels):
        # Determine quadrant
        if rs_ratio > 100 and rs_momentum > 100:
            quadrant = RRGQuadrant.LEADING
        elif rs_ratio < 100 and rs_momentum > 100:
            quadrant = RRGQuadrant.IMPROVING
        elif rs_ratio < 100 and rs_momentum < 100:
            quadrant = RRGQuadrant.LAGGING
        else:  # rs_ratio > 100 and rs_momentum < 100
            quadrant = RRGQuadrant.WEAKENING
        
        # Parse label to extract strike and type
        parts = label.split()
        strike = float(parts[0]) if parts[0].replace('.', '').isdigit() else 0
        option_type = parts[1] if len(parts) > 1 else "CE"
        
        data_points.append(RRGDataPoint(
            strike=strike,
            option_type=option_type,
            rs=0.0,  # Not available from raw data
            rs_ratio=rs_ratio,
            rs_momentum=rs_momentum,
            quadrant=quadrant,
            timestamp=now_ist(),
            option_ltp=0.0,
            benchmark_ltp=0.0,
        ))
    
    snapshot = RRGSnapshot(
        benchmark_symbol="NIFTY",
        benchmark_ltp=0.0,
        timeframe="custom",
        timestamp=now_ist(),
        data_points=data_points,
    )
    
    return visualizer.create_rrg_chart(snapshot, title=title)
