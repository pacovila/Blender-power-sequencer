import bpy
import bgl
import gpu
from gpu_extras.batch import batch_for_shader
from math import floor
from mathutils import Vector

from .utils.find_strips_mouse import find_strips_mouse
from .utils.trim_strips import trim_strips

from .utils.draw import draw_line, draw_arrow_head
from .utils.doc import doc_name, doc_idname, doc_brief, doc_description


SHADER = gpu.shader.from_builtin('2D_UNIFORM_COLOR')


class POWER_SEQUENCER_OT_mouse_cut(bpy.types.Operator):
    """
    *brief* Fast strip cutting based on mouse position


    With this function you can quickly cut and remove a section of strips while keeping or
    collapsing the remaining gap.

    A [video demo](https://youtu.be/GiLmDhmMVAM?t=1m35s) is available.
    """
    doc = {
        'name': doc_name(__qualname__),
        'demo': 'https://i.imgur.com/wVvX4ex.gif',
        'description': doc_description(__doc__),
        'shortcuts': [
            ({'type': 'T', 'value': 'PRESS'},
             {'select_mode': 'smart'},
             {'remove_gaps': False},
             'Trim using the mouse cursor'),
            ({'type': 'T', 'value': 'PRESS', 'alt': True},
             {'select_mode': 'smart'},
             {'remove_gaps': True},
             'Trim using the mouse cursor and remove gaps'),
            ({'type': 'T', 'value': 'PRESS', 'shift': True},
             {'select_mode': 'cursor'},
             {'remove_gaps': False},
             'Trim in all channels'),
            ({'type': 'T', 'value': 'PRESS', 'shift': True, 'alt': True},
             {'select_mode': 'cursor'},
             {'remove_gaps': True},
             'Trim in all channels and remove gaps'),
        ],
        'keymap': 'Sequencer'
    }
    bl_idname = doc_idname(__qualname__)
    bl_label = doc['name']
    bl_description = doc_brief(doc['description'])
    bl_options = {'REGISTER', 'UNDO'}

    select_mode: bpy.props.EnumProperty(
        items=[('cursor', 'Time cursor',
                'Select all of the strips the time cursor overlaps'),
               ('smart', 'Smart',
                'Uses the selection if possible, else uses the other modes')],
        name="Selection mode",
        description="Cut only the strip under the mouse or all strips under the time cursor",
        default='smart')
    select_linked: bpy.props.BoolProperty(
        name="Use linked time",
        description="In mouse or smart mode, always cut linked strips if this is checked",
        default=False)
    remove_gaps: bpy.props.BoolProperty(
        name="Remove gaps",
        description="When trimming the sequences, remove gaps automatically",
        default=True)

    TABLET_TRIM_DISTANCE_THRESHOLD = 6
    mouse_start_x, mouse_start_y = 0.0, 0.0

    frame_start, channel_start = 0, 0
    frame_end, end_channel = 0, 0
    cut_mode = ''
    initially_clicked_strips = None

    mouse_vec_start = Vector([0, 0])
    handle_cut_trim_line = None

    target_strips = []

    event_shift_released = True

    @classmethod
    def poll(cls, context):
        return context.sequences is not None

    def invoke(self, context, event):
        self.mouse_start_x, self.mouse_start_y = event.mouse_region_x, event.mouse_region_y

        frame_float, channel_float = context.region.view2d.region_to_view(
            x=event.mouse_region_x, y=event.mouse_region_y)
        self.frame_start, self.channel_start = round(frame_float), floor(channel_float)
        self.frame_end = self.frame_start

        context.scene.frame_current = self.frame_start

        self.mouse_vec_start = Vector([event.mouse_region_x, event.mouse_region_y])
        self.initially_clicked_strips = find_strips_mouse(
            context, self.frame_start, self.channel_start, select_linked=self.select_linked)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC'}:
            self.remove_draw_handler()
            return {'CANCELLED'}

        # Press Shift to toggle remove gaps
        if event.type in ['LEFT_SHIFT', 'RIGHT_SHIFT']:
            if event.value == 'PRESS' and self.event_shift_released:
                self.event_shift_released = False
                self.select_mode = 'smart' if self.select_mode == 'cursor' else 'cursor'
            elif event.value == 'RELEASE' and not self.event_shift_released:
                self.event_shift_released = True

        elif event.type == 'MOUSEMOVE':
            self.update_time_cursor(context, event)
            self.remove_draw_handler()
            self.update_drawing(context, event)
            return {'PASS_THROUGH'}

        elif event.type == 'RET' or event.type in ['T', 'LEFTMOUSE'] and event.value == 'PRESS':
            distance_to_start = abs(event.mouse_region_x - self.mouse_start_x)
            is_cutting = self.frame_start == self.frame_end or \
                event.is_tablet and distance_to_start <= self.TABLET_TRIM_DISTANCE_THRESHOLD
            if is_cutting:
                self.cut(context)
            else:
                self.trim(context)
            self.remove_draw_handler()
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def update_time_cursor(self, context, event):
        x, y = context.region.view2d.region_to_view(
            x=event.mouse_region_x, y=event.mouse_region_y)
        self.frame_end, self.end_channel = round(x), floor(y)
        context.scene.frame_current = self.frame_end

    def update_drawing(self, context, event):
        to_select, to_delete = self.find_strips_to_trim(context)
        self.target_strips = to_select
        self.target_strips.extend(to_delete)

        # Limit drawing range if trimming a single strip
        draw_end_x = -1
        if self.initially_clicked_strips and not self.select_mode == 'cursor':
            s = self.initially_clicked_strips[0]
            s_frame_start, s_frame_end = s.frame_final_start, s.frame_final_end
            s_x_start = context.region.view2d.view_to_region(s_frame_start, 1)[0]
            s_x_end = context.region.view2d.view_to_region(s_frame_end, 1)[0]
            draw_end_x = max(s_x_start, min(event.mouse_region_x, s_x_end))
        else:
            draw_end_x = event.mouse_region_x

        draw_args = (self, context,
                     Vector([self.mouse_vec_start.x, self.mouse_vec_start.y]),
                     Vector([round(draw_end_x), self.mouse_vec_start.y]),
                     self.remove_gaps)
        self.handle_cut_trim_line = bpy.types.SpaceSequenceEditor.draw_handler_add(
            draw_cut_trim, draw_args, 'WINDOW', 'POST_PIXEL')

    def cut(self, context):
        to_select = self.find_strips_to_cut(context)
        bpy.ops.sequencer.select_all(action='DESELECT')
        for s in to_select:
            s.select = True

        frame_current = context.scene.frame_current
        context.scene.frame_current = self.frame_start
        bpy.ops.sequencer.cut(
            frame=context.scene.frame_current,
            type='SOFT',
            side='BOTH')
        context.scene.frame_current = frame_current


    def trim(self, context):
        to_select, to_delete = self.find_strips_to_trim(context)
        trim_strips(context,
                    self.frame_start, self.frame_end, self.select_mode,
                    to_select, to_delete)
        if self.remove_gaps and self.select_mode == 'cursor':
            context.scene.frame_current = min(self.frame_start, self.frame_end)
            bpy.ops.power_sequencer.remove_gaps()
        else:
            context.scene.frame_current = self.frame_end

    def find_strips_to_cut(self, context):
        """
        Finds and Returns a list of strips to cut
        """
        to_select = []
        overlapping_strips = []
        if self.select_mode == 'smart':
            overlapping_strips = find_strips_mouse(
                context, self.frame_start, self.channel_start, self.select_linked)
            to_select.extend(overlapping_strips)

        if self.select_mode == 'cursor' or (not overlapping_strips and
                                            self.select_mode == 'smart'):
            for s in context.sequences:
                if s.lock:
                    continue
                if s.frame_final_start <= self.frame_start <= s.frame_final_end:
                    to_select.append(s)
        return to_select

    def find_strips_to_trim(self, context):
        """
        Finds and Returns two lists of strips to trim and strips to delete
        """
        to_select, to_delete = [], []
        # overlapping_strips = []
        trim_start, trim_end = min(self.frame_start, self.frame_end), max(
            self.frame_start, self.frame_end)

        if self.select_mode == 'smart':
            to_select.extend(self.initially_clicked_strips)

        if self.select_mode == 'cursor' or (not self.initially_clicked_strips and
                                            self.select_mode == 'smart'):
            for s in context.sequences:
                if s.lock:
                    continue

                if trim_start <= s.frame_final_start and trim_end >= s.frame_final_end:
                    to_delete.append(s)
                    continue
                if s.frame_final_start <= trim_start <= s.frame_final_end or \
                   s.frame_final_start <= trim_end <= s.frame_final_end:
                    to_select.append(s)
        return to_select, to_delete

    def remove_draw_handler(self):
        if self.handle_cut_trim_line:
            bpy.types.SpaceSequenceEditor.draw_handler_remove(self.handle_cut_trim_line, 'WINDOW')


def draw_cut_trim(self, context, start, end, draw_arrows=False):
    """
    Draw function to draw the line and arrows that represent the trim
    """
    # find channel Y coordinates
    channel_tops = [start.y]
    channel_bottoms = [start.y]
    for strip in self.target_strips:
        bottom = context.region.view2d.view_to_region(0, floor(strip.channel))[1]
        if bottom == 12000:
            bottom = 0
        channel_bottoms.append(bottom)

        top = context.region.view2d.view_to_region(0, floor(strip.channel) + 1)[1]
        if top == 12000:
            top = 0
        channel_tops.append(top)
    max_top = max(channel_tops)
    min_bottom = min(channel_bottoms)

    if start.x > end.x:
        start, end = end, start

    bgl.glEnable(bgl.GL_BLEND)
    bgl.glLineWidth(2)

    # horizontal line
    draw_line(SHADER, start, end)

    # vertical lines
    draw_line(SHADER, Vector([start.x, min_bottom]), Vector([start.x, max_top]))
    draw_line(SHADER, Vector([end.x, min_bottom]), Vector([end.x, max_top]))

    if draw_arrows:
        first_arrow_center = Vector([start.x + ((end.x - start.x) * 0.25), start.y])
        second_arrow_center = Vector([end.x - ((end.x - start.x) * 0.25), start.y])
        arrow_size = Vector([10, 20])
        draw_arrow_head(SHADER, first_arrow_center, arrow_size)
        draw_arrow_head(SHADER, second_arrow_center, arrow_size, points_right=False)

    bgl.glLineWidth(1)
    bgl.glDisable(bgl.GL_BLEND)

