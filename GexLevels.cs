#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.IO;
using System.Windows.Media;
using System.Xml.Serialization;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

// GEX Levels — GEXYGEN NinjaTrader 8 indicator.
//
// The NT8 twin of gexygen_levels.pine. Pine is sandboxed, so on TradingView the
// levels arrive as a pasted string; NinjaScript can read the disk, so here the
// indicator reads the collector's levels.txt directly and refreshes itself. Same
// levels, same colours, same labels, same zone shading — no paste step.
//
// levels.txt is written by collector.py in NQ points, only when the futures
// quote succeeded (a stale-but-correct file beats a fresh QQQ-space one on an
// NQ chart). Keys:
//   FLIP                 zero gamma
//   CALL_WALL / PUT_WALL primary walls
//   PUT_WALL_ABOVE_ZG    put wall above zero gamma      (secondary, often absent)
//   CALL_WALL_BELOW_ZG   call wall below zero gamma     (secondary, often absent)
//   EM_HI / EM_LO        expected-move edges
//   L<n>C / L<n>P        ladder levels, suffix = dominant gamma side
//   SPOT_NQ              reference spot (not drawn — the chart already has price)
// Lines beginning with '#' are comments.
//
// Levels are naive (textbook dealer assumption) zones of ±~20 NQ pts — context
// beneath order flow, not signals.

namespace NinjaTrader.NinjaScript.Indicators
{
	public enum GexLabelSize { Tiny, Small, Normal, Large }

	public class GexLevels : Indicator
	{
		// one drawn level, resolved from a levels.txt row
		private class Level
		{
			public string Key;
			public string Text;
			public double Price;
			public Brush  Colour;
			public bool   Dashed;
			public bool   Zone;
		}

		private List<Level>					current		= new List<Level>();
		private System.Threading.Timer		refreshTimer;
		private DateTime					lastWriteUtc	= DateTime.MinValue;
		private DateTime					fileStampUtc	= DateTime.MinValue;
		private string						loadError;
		private readonly object				gate			= new object();

		protected override void OnStateChange()
		{
			if (State == State.SetDefaults)
			{
				Description					= "GEXYGEN naive gamma levels, read live from collector.py's levels.txt.";
				Name						= "GEX Levels";
				Calculate					= Calculate.OnEachTick;
				IsOverlay					= true;
				DisplayInDataBox			= false;
				DrawOnPricePanel			= true;
				PaintPriceMarkers			= false;
				IsSuspendedWhileInactive	= false;	// keep refreshing on a quiet chart
				ArePlotsConfigurable		= false;

				LevelsFile			= @"C:\Users\61411\Desktop\Coding\GEX\levels.txt";
				RefreshSeconds		= 30;
				ShowStatus			= true;
				StaleMinutes		= 20;

				ShowZones			= true;
				ZoneWidth			= 20;
				ShowSecondaryWalls	= true;
				ShowLadder			= true;
				ShowExpectedMove	= true;

				LineWidth			= 1;
				LabelSize			= GexLabelSize.Small;
				LabelOffset			= 6;
				ZoneTransparency	= 92;

				// exact Pine palette — keep these in step with gexygen_levels.pine
				FlipBrush		= new SolidColorBrush(Color.FromRgb(0xC9, 0x96, 0x2E));
				CallWallBrush	= new SolidColorBrush(Color.FromRgb(0x00, 0xAC, 0x77));
				PutWallBrush	= new SolidColorBrush(Color.FromRgb(0xBC, 0x20, 0x71));
				PutAboveZgBrush	= new SolidColorBrush(Color.FromRgb(0x8E, 0x2A, 0x5E));
				CallBelowZgBrush= new SolidColorBrush(Color.FromRgb(0x0A, 0x7D, 0x5A));
				ExpMoveBrush	= new SolidColorBrush(Color.FromRgb(0x7A, 0x84, 0x99));
				LadderCallBrush	= new SolidColorBrush(Color.FromRgb(0x3F, 0x6B, 0x5C));
				LadderPutBrush	= new SolidColorBrush(Color.FromRgb(0x6B, 0x3F, 0x55));
				LadderBrush		= new SolidColorBrush(Color.FromRgb(0x55, 0x5E, 0x70));
			}
			else if (State == State.Configure)
			{
				FreezeBrushes();
			}
			else if (State == State.DataLoaded)
			{
				// A timer, not OnBarUpdate alone: outside RTH the chart can go
				// minutes without a tick, and the whole point of the file reader
				// is that levels appear without the user doing anything.
				int period = Math.Max(5, RefreshSeconds) * 1000;
				refreshTimer = new System.Threading.Timer(
					delegate
					{
						try { TriggerCustomEvent(o => Rebuild(false), null); }
						catch (Exception) { /* chart closing — nothing useful to do */ }
					}, null, 750, period);
			}
			else if (State == State.Terminated)
			{
				if (refreshTimer != null)
				{
					refreshTimer.Dispose();
					refreshTimer = null;
				}
			}
		}

		protected override void OnBarUpdate()
		{
			// Deliberately does NOT re-check the file.
			//
			// With Calculate.OnEachTick this runs on every tick, including every
			// historical one, and even the short-circuit path costs a FileInfo stat
			// (~16 us syscall) plus a status redraw. On a Tick Replay load of a few
			// million ticks that is well over a minute of pure stat time before the
			// chart even appears. The timer already re-checks on its own schedule,
			// which is the only clock that matters for a file the collector rewrites
			// every 5 minutes.
		}

		// ------------------------------------------------------------ brushes

		private void FreezeBrushes()
		{
			foreach (Brush b in new[] { FlipBrush, CallWallBrush, PutWallBrush, PutAboveZgBrush,
										CallBelowZgBrush, ExpMoveBrush, LadderCallBrush,
										LadderPutBrush, LadderBrush })
				if (b != null && b.CanFreeze && !b.IsFrozen)
					b.Freeze();
		}

		/// Zone fill at the configured transparency. Pine shades the box with a
		/// transparency value; NT8 wants the alpha baked into the brush, so the
		/// conversion happens here rather than relying on a drawing-tool overload.
		private Brush ZoneFill(Brush source)
		{
			SolidColorBrush scb = source as SolidColorBrush;
			if (scb == null)
				return source;
			int t = Math.Min(99, Math.Max(0, ZoneTransparency));
			byte alpha = (byte)Math.Round(255.0 * (100 - t) / 100.0);
			Color c = scb.Color;
			SolidColorBrush fill = new SolidColorBrush(Color.FromArgb(alpha, c.R, c.G, c.B));
			fill.Freeze();
			return fill;
		}

		// -------------------------------------------------------------- parse

		/// Resolve one levels.txt row to a drawable level, or null to skip it.
		/// Mirrors the Pine parser's branch order exactly so the two indicators
		/// can never disagree about what a key means.
		private Level Resolve(string key, double price)
		{
			if (key == "SPOT_NQ")
				return null;						// the chart already shows price

			if (key == "FLIP")
				return new Level { Key = key, Text = "FLIP " + Fmt(price), Price = price,
								   Colour = FlipBrush, Zone = true };

			if (key == "CALL_WALL")
				return new Level { Key = key, Text = "CALL WALL " + Fmt(price), Price = price,
								   Colour = CallWallBrush, Zone = true };

			if (key == "PUT_WALL")
				return new Level { Key = key, Text = "PUT WALL " + Fmt(price), Price = price,
								   Colour = PutWallBrush, Zone = true };

			if (key == "PUT_WALL_ABOVE_ZG")
				return ShowSecondaryWalls
					? new Level { Key = key, Text = "PUT WALL ▲ZG " + Fmt(price), Price = price,
								  Colour = PutAboveZgBrush, Zone = true }
					: null;

			if (key == "CALL_WALL_BELOW_ZG")
				return ShowSecondaryWalls
					? new Level { Key = key, Text = "CALL WALL ▼ZG " + Fmt(price), Price = price,
								  Colour = CallBelowZgBrush, Zone = true }
					: null;

			if (key == "EM_HI" || key == "EM_LO")
				return ShowExpectedMove
					? new Level { Key = key, Price = price, Colour = ExpMoveBrush, Dashed = true,
								  Text = (key == "EM_HI" ? "EM+ " : "EM- ") + Fmt(price) }
					: null;

			// L<n>C / L<n>P — trailing letter is the dominant gamma side at that
			// strike. A bare L<n> (older collector) draws neutral, as in Pine.
			if (key.Length >= 2 && key[0] == 'L' && ShowLadder)
			{
				string sfx	= key.Substring(key.Length - 1);
				bool   isC	= sfx == "C";
				bool   isP	= sfx == "P";
				string num	= (isC || isP) ? key.Substring(1, key.Length - 2) : key.Substring(1);
				Brush  col	= isC ? LadderCallBrush : isP ? LadderPutBrush : LadderBrush;
				string side	= isC ? " CALL " : isP ? " PUT " : " ";
				return new Level { Key = key, Text = "L" + num + side + Fmt(price), Price = price,
								   Colour = col };
			}

			return null;
		}

		private static string Fmt(double v)
		{
			return v.ToString("#,##0", CultureInfo.InvariantCulture);
		}

		// --------------------------------------------------------------- draw

		private void Rebuild(bool force)
		{
			if (State != State.Realtime && State != State.Historical)
				return;
			if (ChartControl == null)
				return;
			// The timer starts 750 ms after DataLoaded and can therefore fire before
			// any bar has been processed. Draw.TextFixed reaches into the bar series
			// to anchor itself, so without this the very first refresh on a slow-
			// loading or empty chart throws from a threadpool callback.
			if (CurrentBar < 0)
				return;

			// ForceRefresh is deliberately never called while `gate` is held: OnRender
			// takes the same lock on the render thread, so repainting from under it is
			// how this turns into a stall. Do the work, release, then repaint once.
			bool repaint = LoadLevels(force);
			DrawStatus();
			if (repaint)
				ForceRefresh();
		}

		/// Reads and parses levels.txt under the lock. Returns true when what should
		/// be painted actually changed, so the caller knows whether to repaint.
		private bool LoadLevels(bool force)
		{
			lock (gate)
			{
				string path = LevelsFile;
				if (string.IsNullOrWhiteSpace(path))
					return false;

				FileInfo fi;
				try
				{
					fi = new FileInfo(path);
					if (!fi.Exists)
					{
						loadError    = "levels.txt not found: " + path;
						lastWriteUtc = DateTime.MinValue;
						if (current.Count == 0)
							return false;
						current = new List<Level>();	// stale levels must not linger
						return true;
					}
				}
				catch (Exception ex)
				{
					loadError = "levels.txt unreadable: " + ex.Message;
					return false;
				}

				// nothing new and nothing forced — leave the chart alone
				if (!force && fi.LastWriteTimeUtc == lastWriteUtc)
					return false;

				List<Level> levels = new List<Level>();
				try
				{
					string text;
					// FileShare.Delete matters: the collector writes atomically by
					// replacing the file, which fails if a reader holds it exclusively
					using (FileStream fs = new FileStream(path, FileMode.Open, FileAccess.Read,
														  FileShare.ReadWrite | FileShare.Delete))
					using (StreamReader sr = new StreamReader(fs))
						text = sr.ReadToEnd();

					foreach (string raw in text.Split('\n'))
					{
						string line = raw.Trim();
						if (line.Length == 0 || line[0] == '#')
							continue;
						string[] parts = line.Split(',');
						if (parts.Length < 2)
							continue;
						double price;
						if (!double.TryParse(parts[1].Trim(), NumberStyles.Any,
											 CultureInfo.InvariantCulture, out price))
							continue;
						if (price <= 0)
							continue;
						Level lv = Resolve(parts[0].Trim().ToUpperInvariant(), price);
						if (lv != null)
							levels.Add(lv);
					}
					lastWriteUtc = fi.LastWriteTimeUtc;
					fileStampUtc = fi.LastWriteTimeUtc;
					loadError    = null;
				}
				catch (Exception ex)
				{
					// keep whatever is already on the chart — a stale level beats
					// a blank chart — but say so in the status line
					loadError = "levels.txt read failed: " + ex.Message;
					return false;
				}

				// Everything is painted in OnRender — nothing is a drawing object.
				// NT8 renders drawing objects in a pass of their own, so a
				// Draw.HorizontalLine lands ON TOP of anything OnRender paints no
				// matter what order we call things in; that is why the level line was
				// striking through the labels. Owning the whole paint gives us Pine's
				// actual behaviour: the line stops where the label begins.
				// It also removes the stale-tag bookkeeping entirely — a level that
				// disappears from the file simply stops being painted next frame.
				current = levels;
				return true;
			}
		}

		/// The entire visual is painted here — zones, then lines, then labels — so the
		/// paint order is ours to control.
		///
		/// Two NT8 traps drove this. First, Draw.Text with a negative barsAgo (the
		/// direct translation of Pine's lblOff) compiles, does not throw, and renders
		/// nothing, so no try/catch can detect it. Second, drawing objects render in a
		/// pass of their own AFTER an indicator's OnRender, so a Draw.HorizontalLine
		/// always struck through a label painted here. Owning the whole paint fixes
		/// both and gives Pine's real behaviour: the line terminates at the label.
		protected override void OnRender(ChartControl chartControl, ChartScale chartScale)
		{
			base.OnRender(chartControl, chartScale);

			if (ChartPanel == null || RenderTarget == null || chartScale == null)
				return;

			List<Level> snapshot;
			lock (gate)
				snapshot = new List<Level>(current);
			if (snapshot.Count == 0)
				return;

			float size = LabelSize == GexLabelSize.Tiny   ? 10f
					   : LabelSize == GexLabelSize.Small  ? 12f
					   : LabelSize == GexLabelSize.Normal ? 14f
					   : 17f;

			float panelL = ChartPanel.X;
			float panelR = ChartPanel.X + ChartPanel.W;
			float panelT = ChartPanel.Y;
			float panelB = ChartPanel.Y + ChartPanel.H;

			SharpDX.Direct2D1.Brush bg					= null;
			SharpDX.DirectWrite.TextFormat fmt			= null;
			SharpDX.Direct2D1.StrokeStyle dashStroke	= null;
			try
			{
				bg  = ChartControl.Properties.ChartBackground.ToDxBrush(RenderTarget);
				fmt = new SharpDX.DirectWrite.TextFormat(
						NinjaTrader.Core.Globals.DirectWriteFactory, "Arial", size);

				SharpDX.Direct2D1.StrokeStyleProperties dashProps =
					new SharpDX.Direct2D1.StrokeStyleProperties();
				dashProps.DashStyle = SharpDX.Direct2D1.DashStyle.Dash;
				dashStroke = new SharpDX.Direct2D1.StrokeStyle(RenderTarget.Factory, dashProps);

				float lw = Math.Max(1, LineWidth);

				// ---- pass 1: zone bands, behind everything ----
				if (ShowZones && ZoneWidth > 0)
				{
					foreach (Level lv in snapshot)
					{
						if (!lv.Zone)
							continue;
						float yTop = (float)chartScale.GetYByValue(lv.Price + ZoneWidth);
						float yBot = (float)chartScale.GetYByValue(lv.Price - ZoneWidth);
						if (yBot < panelT || yTop > panelB)
							continue;
						SharpDX.Direct2D1.Brush zb = null;
						try
						{
							zb = ZoneFill(lv.Colour).ToDxBrush(RenderTarget);
							RenderTarget.FillRectangle(
								new SharpDX.RectangleF(panelL, yTop, panelR - panelL, yBot - yTop), zb);
						}
						finally { if (zb != null) zb.Dispose(); }
					}
				}

				// ---- passes 2 and 3: measure the label, stop the line short of it,
				// then paint the label on top of its own opaque plate ----
				foreach (Level lv in snapshot)
				{
					float y = (float)chartScale.GetYByValue(lv.Price);
					if (y < panelT - 2 || y > panelB + 2)
						continue;						// off the visible price scale

					SharpDX.DirectWrite.TextLayout layout	= null;
					SharpDX.Direct2D1.Brush fg				= null;
					try
					{
						layout = new SharpDX.DirectWrite.TextLayout(
									NinjaTrader.Core.Globals.DirectWriteFactory,
									lv.Text, fmt, 400f, size * 2f);
						float w		= layout.Metrics.Width;
						float h		= layout.Metrics.Height;
						float x		= panelR - w - 4f - Math.Max(0, LabelOffset);
						float top	= y - h / 2f;

						fg = lv.Colour.ToDxBrush(RenderTarget);

						// line runs from the left edge and STOPS at the label
						float lineEnd = x - 4f;
						if (lineEnd > panelL)
							RenderTarget.DrawLine(new SharpDX.Vector2(panelL, y),
												  new SharpDX.Vector2(lineEnd, y),
												  fg, lw, lv.Dashed ? dashStroke : null);

						RenderTarget.FillRectangle(
							new SharpDX.RectangleF(x - 3f, top - 1f, w + 6f, h + 2f), bg);
						RenderTarget.DrawTextLayout(new SharpDX.Vector2(x, top), layout, fg);
					}
					finally
					{
						if (fg != null)     fg.Dispose();
						if (layout != null) layout.Dispose();
					}
				}
			}
			catch (Exception)
			{
				// never let a render fault kill the indicator mid-session
			}
			finally
			{
				if (dashStroke != null) dashStroke.Dispose();
				if (fmt != null)		fmt.Dispose();
				if (bg != null)			bg.Dispose();
			}
		}

		/// Age-and-failure line. The project rule is that a stale or failed feed is
		/// never silent, and NT8 is the one surface with no status chips of its own.
		private void DrawStatus()
		{
			if (!ShowStatus)
			{
				RemoveDrawObject("GEX_STATUS");
				return;
			}

			string msg;
			Brush  col;
			if (loadError != null)
			{
				msg = "GEX  " + loadError;
				col = Brushes.OrangeRed;
			}
			else
			{
				double mins = (DateTime.UtcNow - fileStampUtc).TotalMinutes;
				bool   old  = mins > Math.Max(1, StaleMinutes);
				msg = string.Format(CultureInfo.InvariantCulture,
									"GEX levels {0:0} min old{1}", mins,
									old ? "  — collector not feeding" : "");
				col = old ? Brushes.OrangeRed : ExpMoveBrush;
			}
			Draw.TextFixed(this, "GEX_STATUS", msg, TextPosition.BottomRight, col,
						   new SimpleFont("Arial", 11), Brushes.Transparent,
						   Brushes.Transparent, 0);
		}

		#region Properties — Data

		[NinjaScriptProperty]
		[Display(Name = "levels.txt path", Order = 1, GroupName = "1 Data",
				 Description = "File written by collector.py. Levels are in NQ points.")]
		public string LevelsFile { get; set; }

		[NinjaScriptProperty]
		[Range(5, 3600)]
		[Display(Name = "Refresh (seconds)", Order = 2, GroupName = "1 Data",
				 Description = "How often to re-check the file. The collector writes every 5 min.")]
		public int RefreshSeconds { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "Show status line", Order = 3, GroupName = "1 Data",
				 Description = "Bottom-right age readout; turns amber when the feed stalls.")]
		public bool ShowStatus { get; set; }

		[NinjaScriptProperty]
		[Range(1, 1440)]
		[Display(Name = "Stale after (minutes)", Order = 4, GroupName = "1 Data")]
		public int StaleMinutes { get; set; }

		#endregion

		#region Properties — Levels

		[NinjaScriptProperty]
		[Display(Name = "Shade ±zone around flip & walls", Order = 1, GroupName = "2 Levels")]
		public bool ShowZones { get; set; }

		[NinjaScriptProperty]
		[Range(0, double.MaxValue)]
		[Display(Name = "Zone half-width (pts)", Order = 2, GroupName = "2 Levels")]
		public double ZoneWidth { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "Show secondary walls (puts above / calls below zero gamma)",
				 Order = 3, GroupName = "2 Levels")]
		public bool ShowSecondaryWalls { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "Show ladder levels", Order = 4, GroupName = "2 Levels")]
		public bool ShowLadder { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "Show expected-move range", Order = 5, GroupName = "2 Levels")]
		public bool ShowExpectedMove { get; set; }

		#endregion

		#region Properties — Style

		[NinjaScriptProperty]
		[Range(1, 4)]
		[Display(Name = "Line width", Order = 1, GroupName = "3 Style")]
		public int LineWidth { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "Label size", Order = 2, GroupName = "3 Style")]
		public GexLabelSize LabelSize { get; set; }

		// Pine measures this in bars; NT8 paints the label against the panel edge,
		// so pixels are the honest unit here — bars would be a lie about what moves it.
		[NinjaScriptProperty]
		[Range(0, 600)]
		[Display(Name = "Label inset from right edge (px)", Order = 3, GroupName = "3 Style")]
		public int LabelOffset { get; set; }

		[NinjaScriptProperty]
		[Range(0, 99)]
		[Display(Name = "Zone shade transparency", Order = 4, GroupName = "3 Style")]
		public int ZoneTransparency { get; set; }

		#endregion

		#region Properties — Colours

		[XmlIgnore]
		[Display(Name = "Gamma flip", Order = 1, GroupName = "4 Colours")]
		public Brush FlipBrush { get; set; }
		[Browsable(false)]
		public string FlipBrushSerialize
		{ get { return Serialize.BrushToString(FlipBrush); } set { FlipBrush = Serialize.StringToBrush(value); } }

		[XmlIgnore]
		[Display(Name = "Call wall", Order = 2, GroupName = "4 Colours")]
		public Brush CallWallBrush { get; set; }
		[Browsable(false)]
		public string CallWallBrushSerialize
		{ get { return Serialize.BrushToString(CallWallBrush); } set { CallWallBrush = Serialize.StringToBrush(value); } }

		[XmlIgnore]
		[Display(Name = "Put wall", Order = 3, GroupName = "4 Colours")]
		public Brush PutWallBrush { get; set; }
		[Browsable(false)]
		public string PutWallBrushSerialize
		{ get { return Serialize.BrushToString(PutWallBrush); } set { PutWallBrush = Serialize.StringToBrush(value); } }

		[XmlIgnore]
		[Display(Name = "Put wall above zero gamma", Order = 4, GroupName = "4 Colours")]
		public Brush PutAboveZgBrush { get; set; }
		[Browsable(false)]
		public string PutAboveZgBrushSerialize
		{ get { return Serialize.BrushToString(PutAboveZgBrush); } set { PutAboveZgBrush = Serialize.StringToBrush(value); } }

		[XmlIgnore]
		[Display(Name = "Call wall below zero gamma", Order = 5, GroupName = "4 Colours")]
		public Brush CallBelowZgBrush { get; set; }
		[Browsable(false)]
		public string CallBelowZgBrushSerialize
		{ get { return Serialize.BrushToString(CallBelowZgBrush); } set { CallBelowZgBrush = Serialize.StringToBrush(value); } }

		[XmlIgnore]
		[Display(Name = "Expected move", Order = 6, GroupName = "4 Colours")]
		public Brush ExpMoveBrush { get; set; }
		[Browsable(false)]
		public string ExpMoveBrushSerialize
		{ get { return Serialize.BrushToString(ExpMoveBrush); } set { ExpMoveBrush = Serialize.StringToBrush(value); } }

		[XmlIgnore]
		[Display(Name = "Ladder — call-driven", Order = 7, GroupName = "4 Colours")]
		public Brush LadderCallBrush { get; set; }
		[Browsable(false)]
		public string LadderCallBrushSerialize
		{ get { return Serialize.BrushToString(LadderCallBrush); } set { LadderCallBrush = Serialize.StringToBrush(value); } }

		[XmlIgnore]
		[Display(Name = "Ladder — put-driven", Order = 8, GroupName = "4 Colours")]
		public Brush LadderPutBrush { get; set; }
		[Browsable(false)]
		public string LadderPutBrushSerialize
		{ get { return Serialize.BrushToString(LadderPutBrush); } set { LadderPutBrush = Serialize.StringToBrush(value); } }

		[XmlIgnore]
		[Display(Name = "Ladder — side unknown", Order = 9, GroupName = "4 Colours")]
		public Brush LadderBrush { get; set; }
		[Browsable(false)]
		public string LadderBrushSerialize
		{ get { return Serialize.BrushToString(LadderBrush); } set { LadderBrush = Serialize.StringToBrush(value); } }

		#endregion
	}
}
