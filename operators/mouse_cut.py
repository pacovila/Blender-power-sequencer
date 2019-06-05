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
             {'select_mode': 'contextual'},
             {'remove_gaps': False},
             'Trim using the mouse cursor'),
            ({'type': 'T', 'value': 'PRESS', 'alt': True},
             {'select_mode': 'contextual'},
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
               ('contextual', 'Smart',
                'Uses the selection if possible, else uses the other modes')],
        name="Selection mode",
        description="Cut only the strip under the mouse or all strips under the time cursor",
        default='contextual')
    select_linked: bpy.props.BoolProperty(
        name="Use linked time",
        description="In mouse or contextual mode, always cut linked strips if this is checked",
        default=False)
    remove_gaps: bpy.props.BoolProperty(
        name="Remove gaps",
        description="When trimming the sequences, remove gaps automatically",
        default=True)

    TABLET_TRIM_DISTANCE_THRESHOLD = 6
    trim_start, channel_start = 0, 0
    trim_end, end_channel = 0, 0
    is_trimming = False

    handle_cut_trim_line = None

    target_strips = []

    event_shift_released = True
    event_alt_released = True

    @classmethod
    def poll(cls, context):
        return context.sequences is not None

    def invoke(self, context, event):
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC'}:
            return {'CANCELLED'}

        # Press Shift to toggle cursor select mode
        if event.type in ['LEFT_SHIFT', 'RIGHT_SHIFT']:
            if event.value == 'PRESS' and self.event_shift_released:
                self.event_shift_released = False
                self.select_mode = 'contextual' if self.select_mode == 'cursor' else 'cursor'
            elif event.value == 'RELEASE' and not self.event_shift_released:
                self.event_shift_released = True

        # Press Alt to toggle remove gaps
        if event.type in ['LEFT_ALT', 'RIGHT_SHIFT']:
            if event.value == 'PRESS' and self.event_alt_released:
                self.event_alt_released = False
                self.remove_gaps = not self.remove_gaps
            elif event.value == 'RELEASE' and not self.event_alt_released:
                self.event_alt_released = True

        # Start and end trim
        if event.type == 'LEFTMOUSE':

            if event.value == 'PRESS':
                self.trim_start, self.channel_start = get_frame_and_channel(event)
                self.trim_end = self.trim_start
                self.is_trimming = True
                self.drawing_update(context, event)

            if event.value == 'RELEASE':
                self.is_trimming = False

                start_x = context.region.view2d.region_to_view(x=event.mouse_region_x, y=event.mouse_region_y)[0]
                distance_to_start = abs(event.mouse_region_x - start_x)

                is_cutting = self.trim_start == self.trim_end or \
                    event.is_tablet and distance_to_start <= self.TABLET_TRIM_DISTANCE_THRESHOLD
                if is_cutting:
                    self.cut(context)
                else:
                    self.trim(context)
                self.drawing_handler_remove()

        if event.type == 'MOUSEMOVE':
            self.update_time_cursor(context, event)
            if self.is_trimming:
                self.trim_end = context.scene.frame_current
            return {'PASS_THROUGH'}

        if event.type in ['RET', 'T'] and event.value == 'PRESS':
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def update_time_cursor(self, context, event):
        self.trim_end = get_frame_and_channel(event)[0]
        context.scene.frame_current = self.trim_end

    def drawing_update(self, context, event):
        to_select, to_delete = self.find_strips_to_trim(context)
        self.target_strips = to_select + to_delete

        # Limit drawing range if trimming a single strip
        under_mouse = find_strips_mouse(context, self.trim_start, self.channel_start, select_linked=self.select_linked)
        if self.select_mode == 'contextual' and under_mouse:
            s = under_mouse[0]
            strip_start_x = context.region.view2d.view_to_region(s.frame_final_start, 1)[0]
            strip_end_x = context.region.view2d.view_to_region(s.frame_final_end, 1)[0]

            draw_end_x = max(strip_start_x, min(event.mouse_region_x, strip_end_x))
        else:
            draw_end_x = event.mouse_region_x

        start_x = context.region.view2d.view_to_region(self.trim_start, 1)[0]
        draw_args = (self, context,
                     Vector([round(start_x), event.mouse_region_y]),
                     Vector([round(draw_end_x), event.mouse_region_y]),
                     self.remove_gaps)
        self.handle_cut_trim_line = bpy.types.SpaceSequenceEditor.draw_handler_add(
            draw_interaction, draw_args, 'WINDOW', 'POST_PIXEL')

    def drawing_handler_remove(self):
        if self.handle_cut_trim_line:
            bpy.types.SpaceSequenceEditor.draw_handler_remove(self.handle_cut_trim_line, 'WINDOW')

    def cut(self, context):
        to_select = self.find_strips_to_cut(context)
        bpy.ops.sequencer.select_all(action='DESELECT')
        for s in to_select:
            s.select = True

        frame_current = context.scene.frame_current
        context.scene.frame_current = self.trim_start
        bpy.ops.sequencer.cut(
            frame=context.scene.frame_current,
            type='SOFT',
            side='BOTH')
        context.scene.frame_current = frame_current

    def trim(self, context):
        to_select, to_delete = self.find_strips_to_trim(context)
        trim_strips(context,
                    self.trim_start, self.trim_end, self.select_mode,
                    to_select, to_delete)
        if self.remove_gaps and self.select_mode == 'cursor':
            context.scene.frame_current = min(self.trim_start, self.trim_end)
            bpy.ops.power_sequencer.remove_gaps()
        else:
            context.scene.frame_current = self.trim_end

    def find_strips_to_cut(self, context):
        """
        Finds and Returns a list of strips to cut
        """
        to_select = []
        overlapping_strips = []
        if self.select_mode == 'contextual':
            overlapping_strips = find_strips_mouse(
                context, self.trim_start, self.channel_start, self.select_linked)
            to_select.extend(overlapping_strips)

        if self.select_mode == 'cursor' or (not overlapping_strips and
                                            self.select_mode == 'contextual'):
            for s in context.sequences:
                if s.lock:
                    continue
                if s.frame_final_start <= self.trim_start <= s.frame_final_end:
                    to_select.append(s)
        return to_select

    def find_strips_to_trim(self, context):
        """
        Finds and Returns two lists of strips to trim and strips to delete
        """
        to_select, to_delete = [], []

        trim_start = min(self.trim_start, self.trim_end)
        trim_end = max(self.trim_start, self.trim_end)

        under_mouse = find_strips_mouse(context, self.trim_start, self.channel_start, select_linked=self.select_linked)
        if self.select_mode == 'contextual':
            to_select.extend(under_mouse)

        elif self.select_mode == 'cursor' or \
           (not self.initially_clicked_strips and self.select_mode == 'contextual'):
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

def draw_interaction(self, context, start=-1, end=-1, draw_arrows=False):
    """
    Draws the line and arrows that represent the trim

    Params:
    - start and end are the start and end of the drawn trim line's vertices in pixels
    """
    assert start >= 0 and end >= 0

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

    # Drawing
    bgl.glEnable(bgl.GL_BLEND)

    bgl.glLineWidth(2)
    draw_line(SHADER, start, end)
    draw_line(SHADER, Vector([start.x, min_bottom]), Vector([start.x, max_top]))
    draw_line(SHADER, Vector([end.x, min_bottom]), Vector([end.x, max_top]))

    if draw_arrows:
        first_arrow_center = Vector([start.x + ((end.x - start.x) * 0.25), start.y])
        second_arrow_center = Vector([end.x - ((end.x - start.x) * 0.25), start.y])
        arrow_size = Vector([10, 20])

        bgl.glLineWidth(4)
        draw_arrow_head(SHADER, first_arrow_center, arrow_size)
        draw_arrow_head(SHADER, second_arrow_center, arrow_size, points_right=False)

    bgl.glLineWidth(1)
    bgl.glDisable(bgl.GL_BLEND)


def get_frame_and_channel(event):
    """
    Returns a tuple of (frame, channel)
    """
    frame_float, channel_float = bpy.context.region.view2d.region_to_view(
        x=event.mouse_region_x, y=event.mouse_region_y)
    return round(frame_float), round(channel_float)
