#include "view_geometry.hpp"
#include <FL/Fl.H>
#include <FL/Fl_Double_Window.H>
#include <FL/Fl_Button.H>
#include <FL/Fl_Check_Button.H>
#include <FL/Fl_Choice.H>
#include <FL/Fl_Box.H>
#include <FL/Fl_Multiline_Output.H>
#include <FL/Fl_Text_Display.H>
#include <FL/Fl_Text_Buffer.H>
#include <FL/Fl_Menu_Item.H>
#include <FL/Fl_Native_File_Chooser.H>
#include <FL/Fl_Value_Input.H>
#include <FL/Fl_Slider.H>
#include <FL/Fl_Image_Surface.H>
#include <FL/Fl_Image.H>
#include <FL/fl_draw.H>
#include <cstring>
#include <filesystem>
#include <memory>
#include <unordered_map>

#ifdef _WIN32
#define API extern "C" __declspec(dllexport)
#else
#define API extern "C" __attribute__((visibility("default")))
#endif
using Open=int(*)(const char*,double*,char*,int);
using Read=int(*)(int,int,int,int,int,int,unsigned char*,char*,int);
using Save=int(*)(const char*,int,int,const unsigned char*,char*,int);
using Macro=int(*)(int,int*,unsigned char*);
using RGB=std::vector<unsigned char>;
using namespace view;
namespace fs=std::filesystem;
struct Item {std::string path;Pose pose;int frame=0;bool visited=false;std::vector<Mark> marks;};
struct Bitmap {RGB rgb;int w=0,h=0;double step=1;};
static Fl_Color color(unsigned c){return fl_rgb_color(c>>16,(c>>8)&255,c&255);}
static void line(Point a,Point b){fl_line((int)std::round(a.x),(int)std::round(a.y),(int)std::round(b.x),(int)std::round(b.y));}
struct Viewer;
struct Window:Fl_Double_Window {
    Viewer* owner=nullptr;
    Window():Fl_Double_Window(1120,720,"EasyPath | Slide Viewer"){}
    void resize(int,int,int,int) override;
    int handle(int) override;
};
struct Canvas:Fl_Widget {
    Viewer& v;
    Canvas(Viewer& v):Fl_Widget(0,52,1120,638),v(v){}
    void draw() override;int handle(int e) override;
};
struct Map:Fl_Widget {
    Viewer& v;
    Map(Viewer& v):Fl_Widget(0,0,184,132),v(v){}
    void draw() override;int handle(int e) override;
};
static void modal(Fl_Window& win){
    win.set_modal();win.show();
    while(win.shown())Fl::wait();
}
static std::string zoom_text(int z){
    char b[80];std::snprintf(b,sizeof b,"Z%d  |  %.4gx",z,std::ldexp(40.,z-12));return b;
}
struct ToolButton:Fl_Button {
    enum Icon{Preview,Info,Ruler,Download,Full,PointTool,PolygonTool,TagTool,WordTool,Layers,SaveAnnotations} icon;
    ToolButton(Icon i,const char* label):Fl_Button(0,0,38,34,label),icon(i){}
    void draw() override{
        draw_box(value()?fl_down(box()):box(),value()?selection_color():color());
        int cx=x()+w()/2,cy=y()+h()/2;
        fl_color(active_r() ? ::color(0x263a47) : ::color(0x8c979d));fl_line_style(FL_SOLID,2);
        auto stroke=[&](int a,int b,int c,int d){fl_line(cx+a,cy+b,cx+c,cy+d);};
        if(icon==Preview){fl_rect(cx-9,cy-7,19,15);stroke(-8,5,-2,-1);stroke(-2,-1,1,2);stroke(1,2,5,-3);stroke(5,-3,8,0);}
        if(icon==Info){fl_arc(cx-8,cy-8,17,17,0,360);stroke(0,-1,0,5);fl_rectf(cx-1,cy-5,3,3);}
        if(icon==PointTool){fl_arc(cx-6,cy-6,13,13,0,360);fl_pie(cx-2,cy-2,5,5,0,360);}
        if(icon==PolygonTool){stroke(-8,5,-4,-7);stroke(-4,-7,8,-3);stroke(8,-3,5,7);stroke(5,7,-8,5);
            for(Point p:std::array<Point,4>{{{-8,5},{-4,-7},{8,-3},{5,7}}})fl_rectf(cx+(int)p.x-2,cy+(int)p.y-2,4,4);}
        if(icon==TagTool){stroke(-8,-7,0,-7);stroke(0,-7,9,2);stroke(9,2,2,9);stroke(2,9,-8,-1);stroke(-8,-1,-8,-7);fl_pie(cx-5,cy-5,3,3,0,360);}
        if(icon==WordTool){fl_font(FL_HELVETICA_BOLD,16);fl_draw("Aa",cx-10,cy+6);}
        if(icon==SaveAnnotations){fl_rect(cx-8,cy-8,17,17);fl_rect(cx-4,cy-8,8,6);fl_rect(cx-5,cy+1,11,8);}
        if(icon==Layers){stroke(-9,-3,0,-8);stroke(0,-8,9,-3);stroke(9,-3,0,2);stroke(0,2,-9,-3);
            for(int d:{2,6}){stroke(-9,d,0,d+5);stroke(0,d+5,9,d);}}
        if(icon==Ruler){stroke(-9,4,5,-9);stroke(5,-9,10,-4);stroke(10,-4,-4,9);stroke(-4,9,-9,4);
            for(int i=0;i<3;i++)stroke(-5+4*i,1-4*i,-3+4*i,3-4*i);}
        if(icon==Download){stroke(0,-8,0,3);stroke(-4,-1,0,3);stroke(0,3,4,-1);stroke(-8,3,-8,7);stroke(-8,7,8,7);stroke(8,7,8,3);}
        if(icon==Full){for(int a:{-1,1})for(int b:{-1,1}){stroke(a*8,b*8,a*3,b*8);stroke(a*8,b*8,a*8,b*3);}}
        fl_line_style(0);if(Fl::focus()==this)draw_focus();
    }
};
struct Viewer {
    Window win;
    Canvas canvas{*this};
    Fl_Button add{12,10,112,32,"Open slides..."};
    Fl_Choice slides{172,10,380,32,"Slide"},frames{618,10,124,32,"Frame"};
    ToolButton preview{ToolButton::Preview,"Slide preview"},metadata{ToolButton::Info,"Slide/image metadata"},measure{ToolButton::Ruler,"Measure"},
        download{ToolButton::Download,"Download"},full{ToolButton::Full,"Fullscreen"};
    Fl_Box nav{0,0,260,86};
    Fl_Button minus{0,0,30,26,"-"},plus{0,0,30,26,"+"},fit{0,0,44,26,"Fit"};
    Fl_Box zoom_label{0,0,118,26};
    Fl_Slider rotation{0,0,88,24};
    Fl_Value_Input angle{0,0,56,24};
    Fl_Button zero{0,0,36,24,"0"};
    Fl_Button toggle_map{0,0,84,24,"Hide map"};Map map{*this};
    Fl_Box status{0,0,1100,28};
    Open open;Read read;Save save;Macro macro=nullptr;
    std::vector<Item> items;int active=-1,count=0;
    std::unordered_map<std::string,int> indices;
    std::vector<std::string> slide_labels;
    std::vector<Fl_Menu_Item> slide_menu;
    int max_read_z=16;
    double width=0,height=0,mx=0,my=0;
    std::array<char,65536> msg{};std::string info;
    RGB pixels;int pw=0,ph=0;bool dirty=true,overview_dirty=true;
    Bitmap overview;
    bool defer_map=false, map_pending=false;
    static void finish_map(void* data){
        auto& v=*static_cast<Viewer*>(data);
        v.map_pending=false;
        if(!v.win.shown()||!v.show_map||!v.map.visible_r()||!v.overview_dirty||!v.defer_map)return;
        v.overview=v.thumbnail(v.map.w()*2,v.map.h()*2);
        v.overview_dirty=false;v.map.redraw();
    }
    void cancel_map(){
        if(map_pending){Fl::remove_timeout(finish_map,this);map_pending=false;}
    }
    void invalidate_map(){
        cancel_map();overview=Bitmap{};overview_dirty=true;
    }
    std::vector<Point> draft;Point cursor,press,pan_center;bool dragging=false,drag_measure=false;
    int ink=0;bool show_map=true;
    Viewer(Open o,Read r,Save s):open(o),read(r),save(s){
        win.end();win.owner=this;win.size_range(780,480);
        nav.box(FL_FLAT_BOX);nav.color(color(0xf0f3f5));
        rotation.type(FL_HOR_NICE_SLIDER);rotation.range(-180,180);angle.range(-180,180);
        rotation.step(.01);angle.step(.01);angle.precision(2);
        rotation.tooltip("Rotation (degrees)");angle.tooltip("Enter rotation in degrees");zero.tooltip("Reset rotation");
        nav.align(FL_ALIGN_INSIDE|FL_ALIGN_BOTTOM_LEFT);nav.labelsize(12);
        zoom_label.labelfont(FL_HELVETICA_BOLD);zoom_label.labelsize(13);
        status.align(FL_ALIGN_LEFT|FL_ALIGN_INSIDE|FL_ALIGN_CLIP);status.labelsize(13);
        measure.type(FL_TOGGLE_BUTTON);measure.selection_color(color(0x9cc6ff));
        for(auto* b:std::vector<Fl_Button*>{&preview,&metadata,&measure,&download,&full,&toggle_map,&minus,&plus,&zero,&fit,&add}){
            b->box(FL_THIN_UP_BOX);b->color(color(0xf4f6f8));b->labelsize(13);
            b->callback([](Fl_Widget*w,void*u){static_cast<Viewer*>(u)->action(w);},this);
        }
        preview.tooltip("Slide preview");metadata.tooltip("Slide/image metadata");
        measure.tooltip("Measure physical length");download.tooltip("Download current view");full.tooltip("Toggle fullscreen (F11)");
        minus.tooltip("Zoom out");plus.tooltip("Zoom in");fit.tooltip("Fit the slide at its current rotation");
        slides.callback([](Fl_Widget*,void*u){auto& v=*(Viewer*)u;v.select(v.slides.value());},this);
        frames.callback([](Fl_Widget*,void*u){auto& v=*(Viewer*)u;v.frame(v.frames.value());},this);
        for(Fl_Valuator* w:std::array<Fl_Valuator*,2>{&rotation,&angle})
            w->callback([](Fl_Widget*w,void*u){((Viewer*)u)->rotate(((Fl_Valuator*)w)->value());},this);
        angle.when(FL_WHEN_ENTER_KEY|FL_WHEN_RELEASE);
        for(Fl_Widget* w:std::vector<Fl_Widget*>{&slides,&frames,&preview,&metadata,&measure,&download,&rotation,&angle,&zero,&minus,&plus,&fit,&toggle_map})w->deactivate();
        message("Open local whole-slide files.");layout();
    }
    virtual ~Viewer(){cancel_map();win.hide();win.owner=nullptr;slides.menu(nullptr);}
    Item& it(){return items.at(active);}
    virtual bool editing() const{return false;}
    virtual bool leave_draft(){return true;}
    virtual void opened(){}
    virtual int edit_event(int,Point,Point){return 0;}
    virtual void paint_annotations(Point,bool){}
    Transform tr(){return Transform(it().pose,canvas.w(),canvas.h());}
    void message(const std::string& s){status.copy_label(s.c_str());}
    virtual void layout(){
        int w=win.w(),h=win.h(),bottom=h-27;
        canvas.resize(0,52,w,bottom-52);status.resize(12,bottom,w-24,27);
        preview.position(12,64);metadata.position(55,64);
        full.position(w-50,64);download.position(w-93,64);measure.position(w-136,64);
        nav.position(12,bottom-95);int n=nav.y();
        minus.position(20,n+7);zoom_label.position(52,n+7);plus.position(172,n+7);fit.position(212,n+7);
        rotation.label("Rotate");rotation.labelsize(12);rotation.resize(65,n+45,82,24);angle.position(153,n+44);zero.position(217,n+44);
        map.position(w-198,bottom-146);toggle_map.position(w-98,show_map?map.y()-28:bottom-35);
        dirty=true;win.redraw();
    }
    void refresh(bool reread=true){
        dirty|=reread;win.redraw();
        if(active>=0){
            zoom_label.copy_label(zoom_text(it().pose.z).c_str());
            rotation.value(it().pose.degrees);angle.value(it().pose.degrees);
        }
    }
    void clamp_center(){auto& p=it().pose.center;p={std::clamp(p.x,0.,width),std::clamp(p.y,0.,height)};}
    void rotate(double a){if(active<0||!std::isfinite(a))return;it().pose.degrees=std::clamp(a,-180.,180.);refresh();}
    void reset(){
        if(active<0)return;
        auto t=tr();double w=std::abs(t.c)*width+std::abs(t.s)*height,h=std::abs(t.s)*width+std::abs(t.c)*height;
        it().pose.center={width*.5,height*.5};
        it().pose.z=std::clamp(12-(int)std::ceil(std::log2(std::max(w/canvas.w(),h/canvas.h()))),-12,16);refresh();
    }
    void zoom(int n,Point p){
        if(active<0)return;auto anchor=tr().world(p);
        it().pose.z=std::clamp(it().pose.z+n,-12,16);
        it().pose.center=it().pose.center+(anchor-tr().world(p));clamp_center();refresh();
    }
    int append_batch(const std::vector<std::string>& paths){
        if(paths.empty())return -1;
        auto previous=items.size();
        for(const auto& path:paths){
            if(!indices.emplace(path,(int)items.size()).second)continue;
            if(items.size()==previous)slides.menu(nullptr);
            items.push_back({path});
            slide_labels.push_back(std::to_string(items.size())+". "+fs::u8path(path).filename().u8string());
        }
        if(items.size()!=previous){
            // FLTK borrows both the menu array and its literal labels.
            slide_menu.assign(items.size()+1,Fl_Menu_Item{});
            for(size_t i=0;i<items.size();i++)slide_menu[i].label(slide_labels[i].c_str());
            slides.menu(slide_menu.data());
            if(active>=0)slides.value(active);else slides.value(static_cast<const Fl_Menu_Item*>(nullptr));
            slides.activate();
        }
        return indices.at(paths.front());
    }
    void append(const std::string& path){append_batch({path});}
    void clear_draft(){draft.clear();drag_measure=false;dragging=false;}
    bool select(int i,bool reload=false){
        if(i<0||i>=(int)items.size())return false;if(i==active&&!reload)return true;
        if(!leave_draft()){slides.value(active);return false;}
        std::array<double,6>d{};msg.fill(0);
        if(open(items[i].path.c_str(),d.data(),msg.data(),msg.size())){slides.value(active);message(msg.data());return false;}
        int previous_count=count;
        active=i;slides.value(i);width=d[0];height=d[1];count=(int)d[2];mx=d[3];my=d[4];info=msg.data();
        auto suffix=fs::u8path(items[i].path).extension().u8string();
        std::transform(suffix.begin(),suffix.end(),suffix.begin(),[](unsigned char c){return char(std::tolower(c));});
        max_read_z=16;
        if(count==1&&suffix==".ndpi"&&d[5]>0&&d[5]<=1){
            // The NDP.read bridge cannot request fractional 40x-reference steps.
            // Use the scanner grid (20x -> Z11, 40x -> Z12), then magnify on the canvas.
            max_read_z=std::clamp(12+(int)std::ceil(std::log2(d[5])),-12,12);
        }
        if(count!=previous_count){
            frames.clear();for(int k=0;k<count;k++)frames.add(std::to_string(k+1).c_str());
        }
        it().frame=std::clamp(it().frame,0,count-1);frames.value(it().frame);
        if(count>1)frames.activate();else frames.deactivate();
        for(Fl_Widget* w:std::vector<Fl_Widget*>{&preview,&metadata,&download,&rotation,&angle,&zero,&minus,&plus,&fit,&toggle_map})w->activate();
        measure.value(0);if(mx&&my)measure.activate();else measure.deactivate();
        if(!it().visited){reset();it().visited=true;}else clamp_center();
        defer_map=suffix==".svs";
        clear_draft();invalidate_map();opened();refresh();return true;
    }
    void frame(int f){
        if(active<0||f<0||f>=count||f==it().frame)return;
        if(!leave_draft()){frames.value(it().frame);return;}
        it().frame=f;frames.value(f);clear_draft();invalidate_map();opened();refresh();
    }
    bool page(int n,bool shift){if(active<0)return false;if(shift)frame(it().frame+n);else select(active+n);return true;}
    bool inside(Point p){return p.x>=0&&p.y>=0&&p.x<width&&p.y<height;}
    Bitmap thumbnail(int maxw,int maxh){
        double ratio=std::max(width/maxw,height/maxh);
        int z=std::clamp(12-(int)std::ceil(std::log2(ratio)),-12,16);
        double step=std::ldexp(1.,12-z);Bitmap b;b.step=step;
        b.w=std::max(1,(int)std::ceil(width/step));b.h=std::max(1,(int)std::ceil(height/step));
        if(b.w>maxw||b.h>maxh)return {};
        b.rgb.resize(size_t(b.w)*b.h*3);
        if(read(0,0,z,b.w,b.h,it().frame,b.rgb.data(),msg.data(),msg.size()))return {};
        return b;
    }
    Bitmap glass(){
        Bitmap b;if(!macro)return b;
        constexpr int limit=1024;int dims[2]={};b.rgb.resize(size_t(limit)*limit*3);
        if(macro(limit,dims,b.rgb.data())||dims[0]<1||dims[0]>limit||dims[1]<1||dims[1]>limit)return {};
        b.w=dims[0];b.h=dims[1];b.rgb.resize(size_t(b.w)*b.h*3);return b;
    }
    bool render();
    void overlays(int ox,int oy,bool scale_on,bool marks_on,bool live,bool annotations=false);
    RGB compose(bool scale_on,bool marks_on,bool annotations=false);
    void action(Fl_Widget*);
    void export_view();
    void inspect(bool show_preview);
    void edit_mark(int index);
    int hit(Point s){
        for(int i=(int)it().marks.size()-1;i>=0;i--){
            auto& m=it().marks[i];if(m.frame!=it().frame)continue;auto t=tr();
            for(size_t k=1;k<m.points.size();k++)if(hit_distance(s,t.screen(m.points[k-1]),t.screen(m.points[k]))<=7)return i;
        }return -1;
    }
    void finish_mark(Point p,bool extend){
        if(draft.empty()){draft.push_back(p);cursor=p;return;}
        auto d=p-draft.back();if(std::hypot(d.x,d.y)<1e-8)return;
        draft.push_back(p);cursor=p;
        if(!extend){
            it().marks.push_back({it().frame,ink,draft});message("Length: "+metric(length(draft,mx,my)));
            clear_draft();measure.value(0);
        }
    }
};

bool Viewer::render(){
    if(active<0)return false;
    if(!dirty)return !pixels.empty();
    dirty=false;pw=canvas.w();ph=canvas.h();pixels.clear();
    if(pw<=0||ph<=0||size_t(pw)*ph>16777216){message("Viewport exceeds the rendering limit.");return false;}
    pixels.assign(size_t(pw)*ph*3,37);
    auto t=tr();int read_z=std::min(it().pose.z,max_read_z);
    double step=std::ldexp(1.,12-read_z);
    int nx=(int)std::ceil(width/step),ny=(int)std::ceil(height/step);
    int align=std::max(1,(int)std::round(1/step));
    for(int y=0;y<ph;y+=384)for(int x=0;x<pw;x+=384){
        int endx=std::min(x+384,pw),endy=std::min(y+384,ph);
        double lx=width,ly=height,hx=0,hy=0;
        for(int a:{x,endx})for(int b:{y,endy}){
            auto p=t.world({double(a),double(b)});lx=std::min(lx,p.x);ly=std::min(ly,p.y);hx=std::max(hx,p.x);hy=std::max(hy,p.y);
        }
        if(hx<=0||hy<=0||lx>=width||ly>=height)continue;
        int sx=std::max(0,(int)std::floor(lx/step-.5))/align*align,sy=std::max(0,(int)std::floor(ly/step-.5))/align*align;
        int sw=std::min(nx-1,(int)std::ceil(hx/step-.5))-sx+1,sh=std::min(ny-1,(int)std::ceil(hy/step-.5))-sy+1;
        if(sw<1||sh<1)continue;
        RGB tile(size_t(sw)*sh*3);
        if(read((int)std::round(sx*step),(int)std::round(sy*step),read_z,sw,sh,it().frame,tile.data(),msg.data(),msg.size())){
            pixels.clear();message(msg.data());return false;
        }
        for(int j=y;j<endy;j++)for(int i=x;i<endx;i++){
            auto p=t.world({i+.5,j+.5});if(!inside(p))continue;
            double u=std::clamp(p.x/step-.5-sx,0.,double(sw-1)),v=std::clamp(p.y/step-.5-sy,0.,double(sh-1));
            int a=(int)u,b=(int)v,a1=std::min(a+1,sw-1),b1=std::min(b+1,sh-1);u-=a;v-=b;
            auto dst=&pixels[(size_t(j)*pw+i)*3];auto src=&tile[(size_t(b)*sw+a)*3];
            if(u==0&&v==0){std::memcpy(dst,src,3);continue;}
            for(int k=0;k<3;k++){
                auto at=[&](int m,int n){return tile[(size_t(n)*sw+m)*3+k];};
                dst[k]=(unsigned char)std::round((1-v)*((1-u)*src[k]+u*at(a1,b))+v*((1-u)*at(a,b1)+u*at(a1,b1)));
            }
        }
    }
    return true;
}
static void chip(Point a,const std::string& s){
    fl_font(FL_HELVETICA_BOLD,13);int x=(int)a.x,y=(int)a.y,w=(int)std::ceil(fl_width(s.c_str()));
    fl_color(color(0x22282e));fl_rectf(x-4,y-15,w+8,20);fl_color(FL_WHITE);fl_draw(s.c_str(),x,y);
}
void Viewer::overlays(int ox,int oy,bool scale_on,bool marks_on,bool live,bool annotations){
    auto t=tr();Point origin{double(ox),double(oy)};
    fl_push_clip(ox,oy,pw,ph);
    if(live||annotations)paint_annotations(origin,live);
    auto paint=[&](const std::vector<Point>& pts,int ink){
        if(pts.empty())return;
        fl_color(color(colors.at(ink)));fl_line_style(FL_SOLID,3);
        Point prev=t.screen(pts[0])+origin;
        for(auto pt:pts){auto cur=t.screen(pt)+origin;line(prev,cur);fl_pie((int)cur.x-3,(int)cur.y-3,7,7,0,360);prev=cur;}
        fl_line_style(0);
        if(pts.size()>1 &&prev.x>=ox&&prev.x<ox+pw&&prev.y>=oy&&prev.y<oy+ph){
            auto label=metric(length(pts,mx,my));fl_font(FL_HELVETICA_BOLD,13);
            chip({std::clamp(prev.x+9,double(ox+5),double(std::max(ox+5,ox+pw-(int)fl_width(label.c_str())-6))),
                  std::clamp(prev.y-7,double(oy+17),double(oy+ph-5))},label);
        }
    };
    if(marks_on){
        for(const auto& m:it().marks)if(m.frame==it().frame)paint(m.points,m.color);
        if(live&&!draft.empty()){auto tmp=draft;tmp.push_back(cursor);paint(tmp,ink);}
    }
    auto bar=scale(t,mx,my);
    if(scale_on&&bar.px>0){
        int cx=ox+pw/2,y=oy+ph-15,l=(int)std::round(cx-bar.px/2),r=(int)std::round(cx+bar.px/2);
        std::string s=metric(bar.um);fl_font(FL_HELVETICA_BOLD,13);
        chip({cx-fl_width(s.c_str())/2,double(y-8)},s);
        for(int thick:{5,2}){fl_color(thick==5?FL_BLACK:FL_WHITE);fl_line_style(FL_SOLID,thick);fl_line(l,y,r,y);fl_line(l,y-5,l,y+1);fl_line(r,y-5,r,y+1);}
    }
    fl_line_style(0);fl_pop_clip();
}
RGB Viewer::compose(bool scale_on,bool marks_on,bool annotations){
    if(!render())return {};
    if(!scale_on&&!marks_on&&!annotations)return pixels;
    Fl_Image_Surface surface(pw,ph,0);Fl_Surface_Device::push_current(&surface);
    fl_draw_image(pixels.data(),0,0,pw,ph,3);overlays(0,0,scale_on,marks_on,false,annotations);
    std::unique_ptr<Fl_RGB_Image> im(surface.image());Fl_Surface_Device::pop_current();
    if(!im||im->data_w()!=pw||im->data_h()!=ph||im->d()<3)return {};
    RGB out(pixels.size());int d=im->d(),ld=im->ld()?im->ld():pw*d;
    for(int j=0;j<ph;j++){auto row=(const unsigned char*)im->data()[0]+size_t(j)*ld;
        if(d==3)std::memcpy(&out[size_t(j)*pw*3],row,pw*3);
        else for(int i=0;i<pw;i++)std::memcpy(&out[(size_t(j)*pw+i)*3],row+size_t(i)*d,3);
    }return out;
}
void Canvas::draw(){
    fl_push_clip(x(),y(),w(),h());fl_color(::color(0x252525));fl_rectf(x(),y(),w(),h());
    if(v.render()){fl_draw_image(v.pixels.data(),x(),y(),v.pw,v.ph,3);v.overlays(x(),y(),true,true,true);}
    fl_pop_clip();
}
int Canvas::handle(int e){
    if(v.active<0)return 0;Point p{double(Fl::event_x()-x()),double(Fl::event_y()-y())},world=v.tr().world(p);
    if(v.editing()&&(e==FL_FOCUS||e==FL_UNFOCUS))return 1;
    if(v.editing()&&e==FL_PUSH)take_focus();
    if(v.edit_event(e,p,world))return 1;
    if(e==FL_ENTER)return 1;
    if(e==FL_MOUSEWHEEL){if(Fl::event_dy())v.zoom(Fl::event_dy()<0?1:-1,p);return 1;}
    if(e==FL_PUSH){
        if(Fl::event_button()==FL_RIGHT_MOUSE){int i=v.hit(p);if(i>=0)v.edit_mark(i);return 1;}
        if(Fl::event_button()!=FL_LEFT_MOUSE)return 0;v.press=p;
        if(v.measure.value()){
            if(v.pixels.empty()||!v.inside(world))return 1;
            bool first=v.draft.empty();v.finish_mark(world,Fl::event_state(FL_CTRL));v.drag_measure=first;v.refresh(false);
        }else{v.dragging=true;v.pan_center=v.it().pose.center;}
        return 1;
    }
    if(e==FL_DRAG&&v.dragging){v.it().pose.center=v.pan_center-v.tr().inverse_delta(p-v.press);v.clamp_center();v.refresh();return 1;}
    if((e==FL_MOVE||e==FL_DRAG)&&!v.draft.empty()&&v.inside(world)){v.cursor=world;v.refresh(false);return 1;}
    if(e==FL_RELEASE){
        auto d=p-v.press;
        if(v.drag_measure&&std::hypot(d.x,d.y)>4&&v.inside(world))v.finish_mark(world,Fl::event_state(FL_CTRL));
        v.dragging=v.drag_measure=false;v.refresh(false);return 1;
    }return Fl_Widget::handle(e);
}
static Transform map_transform(Map& m){
    auto& v=m.v;auto t=v.tr();t.center={v.width*.5,v.height*.5};
    t.mid={m.x()+m.w()*.5,m.y()+m.h()*.5};
    t.step=std::max((std::abs(t.c)*v.width+std::abs(t.s)*v.height)/(m.w()-8),
                    (std::abs(t.s)*v.width+std::abs(t.c)*v.height)/(m.h()-8));
    return t;
}
// Map raster, viewport outline, and pointer navigation use the same projection.
static RGB map_image(Map& m,const Transform& t){
    auto& b=m.v.overview;RGB rgb(size_t(m.w())*m.h()*3,37);
    if(b.rgb.empty())return rgb;
    for(int y=0;y<m.h();y++)for(int x=0;x<m.w();x++){
        auto p=t.world({m.x()+x+.5,m.y()+y+.5});if(!m.v.inside(p))continue;
        int i=std::min(b.w-1,(int)(p.x/b.step)),j=std::min(b.h-1,(int)(p.y/b.step));
        std::memcpy(&rgb[(size_t(y)*m.w()+x)*3],&b.rgb[(size_t(j)*b.w+i)*3],3);
    }return rgb;
}
void Map::draw(){
    if(v.active<0)return;
    if(v.overview_dirty && v.defer_map){
        // Let the double-buffered window present the tissue before decoding the map.
        if(!v.map_pending && !v.dirty && !v.pixels.empty() && v.win.shown()){
            v.map_pending=true;Fl::add_timeout(0.0,Viewer::finish_map,&v);
        }
    }else if(v.overview_dirty){v.overview=v.thumbnail(w()*2,h()*2);v.overview_dirty=false;}
    auto mt=map_transform(*this);auto rgb=map_image(*this,mt);
    fl_push_clip(x(),y(),w(),h());fl_draw_image(rgb.data(),x(),y(),w(),h(),3);
    if(v.overview.rgb.empty()){fl_color(FL_WHITE);fl_font(FL_HELVETICA,11);fl_draw(v.map_pending?"Loading overview":"Overview unavailable",x(),y(),w(),h(),FL_ALIGN_CENTER);}
    auto pos=[&](Point p){return mt.screen(p);};
    auto t=v.tr();double cw=v.canvas.w(),ch=v.canvas.h();
    std::array<Point,4> corners{{{0,0},{cw,0},{cw,ch},{0,ch}}};
    fl_color(::color(0x3095ff));fl_line_style(FL_SOLID,2);
    for(int i=0;i<4;i++)line(pos(t.world(corners[i])),pos(t.world(corners[(i+1)%4])));
    fl_line_style(0);auto c=pos(t.center);fl_pie((int)c.x-3,(int)c.y-3,7,7,0,360);
    fl_pop_clip();fl_color(::color(0x8c9aa3));fl_rect(x(),y(),w(),h());
}
int Map::handle(int e){
    if(v.active<0 || (v.defer_map && v.overview.rgb.empty()))return 0;
    if((e==FL_PUSH&&Fl::event_button()==FL_LEFT_MOUSE)||e==FL_DRAG){
        auto p=map_transform(*this).world({Fl::event_x()+.5,Fl::event_y()+.5});
        if(e==FL_PUSH&&!v.inside(p))return 0;
        v.it().pose.center=p;v.clamp_center();v.refresh();return 1;
    }return Fl_Widget::handle(e);
}
void Viewer::edit_mark(int i){
    if(i<0||i>=(int)it().marks.size())return;
    std::array<Fl_Menu_Item,12> menu{};
    for(int k=0;k<10;k++){menu[k].label(names[k]);menu[k].labelcolor(color(colors[k]==0xffffff?0x676767:colors[k]));}
    menu[9].flags=FL_MENU_DIVIDER;menu[10].label("Delete measurement");
    auto chosen=menu[0].popup(Fl::event_x(),Fl::event_y());if(!chosen)return;
    auto k=chosen-menu.data();
    if(k==10)it().marks.erase(it().marks.begin()+i);else it().marks[i].color=(int)k;
    refresh(false);
}
static void close_popup(Fl_Widget*,void* p){((Fl_Window*)p)->hide();}
void Viewer::inspect(bool show_preview){
    Bitmap b;if(show_preview)b=glass();
    Fl_Window w(680,526,show_preview?"Slide preview | Glass slide":"Slide/image metadata");
    Fl_Box box(18,18,644,438);std::unique_ptr<Fl_RGB_Image> img;
    Fl_Text_Buffer buffer;Fl_Text_Display details(18,18,644,438);details.buffer(buffer);details.textsize(14);
    details.wrap_mode(Fl_Text_Display::WRAP_AT_BOUNDS,0);
    if(show_preview){
        details.hide();box.box(FL_FLAT_BOX);box.color(color(0x252525));
        if(!b.rgb.empty()){img=std::make_unique<Fl_RGB_Image>(b.rgb.data(),b.w,b.h,3);
            double q=std::min(640./b.w,432./b.h);img->scale(std::max(1,(int)(b.w*q)),std::max(1,(int)(b.h*q)),0,1);box.image(img.get());}
        else{box.label("No scanner macro or label is available for this slide.");box.labelcolor(FL_WHITE);}
    }else{box.hide();buffer.text(info.c_str());}
    Fl_Button close(568,474,94,32,"Close");close.callback(close_popup,&w);w.end();modal(w);box.image(nullptr);
}
void Viewer::export_view(){
    if(!render())return;
    Fl_Window w(400,232,"Download current view");
    Fl_Choice format(112,23,244,31,"Format");format.add("PNG");format.add("JPEG");format.value(0);
    Fl_Check_Button bar(24,72,348,27,"Include scale bar");if(mx<=0||my<=0)bar.deactivate();
    Fl_Check_Button marks(24,108,348,27,"Include measurements and lengths");
    Fl_Check_Button annotations(24,139,348,27,"Include visible annotations");
    if(editing())annotations.value(1);else annotations.hide();
    if(!std::any_of(it().marks.begin(),it().marks.end(),[&](auto&m){return m.frame==it().frame;}))marks.deactivate();
    bool accept=false;Fl_Button cancel(154,173,97,32,"Cancel"),go(260,173,116,32,"Save as...");
    cancel.callback(close_popup,&w);go.callback([](Fl_Widget* b,void* p){*(bool*)p=true;b->window()->hide();},&accept);
    w.end();modal(w);if(!accept)return;
    bool jpg=format.value()==1;Fl_Native_File_Chooser file(Fl_Native_File_Chooser::BROWSE_SAVE_FILE);
    file.title("Save current view");file.filter(jpg?"JPEG image\t*.jpg":"PNG image\t*.png");
    file.preset_file(jpg?"slide-view.jpg":"slide-view.png");if(file.show()!=0||!file.filename())return;
    std::string path=file.filename(),ext=fs::u8path(path).extension().u8string();
    std::transform(ext.begin(),ext.end(),ext.begin(),[](unsigned char c){return char(std::tolower(c));});
    if(ext.empty())path+=jpg?".jpg":".png";
    else if(jpg?(ext!=".jpg"&&ext!=".jpeg"):ext!=".png"){message("The filename extension must match the selected format.");return;}
    auto rgb=compose(bar.value(),marks.value(),annotations.value());if(rgb.empty()){message("Could not render the snapshot.");return;}
    if(save(path.c_str(),pw,ph,rgb.data(),msg.data(),msg.size()))message(msg.data());else message("Saved current view.");
}
void Viewer::action(Fl_Widget* b){
    if(b==&full){
        if(win.fullscreen_active()){win.fullscreen_off();full.label("Fullscreen");}
        else{win.fullscreen();full.label("Exit fullscreen");}return;
    }
    if(b==&add){
        Fl_Native_File_Chooser picker(Fl_Native_File_Chooser::BROWSE_MULTI_FILE);picker.title("Open whole-slide files");
        picker.filter("Whole-slide images\t*.{svs,ndpi,scn,mrxs,bif,svslide,tif,tiff,ep,dcm,dicom,isyntax}");
        if(picker.show()==0&&picker.count()>0){
            std::vector<std::string> paths;paths.reserve(picker.count());
            for(int n=0;n<picker.count();n++)paths.emplace_back(picker.filename(n));
            select(append_batch(paths));
        }return;
    }
    if(active<0)return;
    if(b==&preview||b==&metadata)inspect(b==&preview);
    else if(b==&download)export_view();
    else if(b==&toggle_map){show_map=!show_map;if(show_map)map.show();else {cancel_map();map.hide();}toggle_map.label(show_map?"Hide map":"Show map");layout();}
    else if(b==&measure){clear_draft();message(measure.value()?"Click to start and finish. Ctrl-click adds a point.":"");refresh(false);}
    else if(b==&fit)reset();else if(b==&zero)rotate(0);
    else if(b==&plus||b==&minus)zoom(b==&plus?1:-1,tr().mid);
}
void Window::resize(int X,int Y,int W,int H){bool changed=W!=w()||H!=h();Fl_Double_Window::resize(X,Y,W,H);if(owner&&changed)owner->layout();}
int Window::handle(int e){
    if(owner&&(e==FL_KEYDOWN||e==FL_SHORTCUT)&&!Fl::event_state(FL_CTRL|FL_ALT|FL_META)){
        int k=Fl::event_key();
        if((k==FL_Page_Up||k==FL_Page_Down)&&owner->page(k==FL_Page_Up?1:-1,Fl::event_state(FL_SHIFT)))return 1;
        if(k==FL_F+11){owner->action(&owner->full);return 1;}
        if(k==FL_Escape){if(!owner->draft.empty()||owner->measure.value()){owner->clear_draft();owner->measure.value(0);owner->refresh(false);}
            else if(fullscreen_active())owner->action(&owner->full);return 1;}
    }return Fl_Double_Window::handle(e);
}
API int viewer_show(Open o,Read r,Save s,Macro macro,int smoke){
    Fl::scheme("gtk+");Viewer v(o,r,s);v.macro=macro;v.win.show();
    if(smoke){Fl::check();v.win.hide();return 0;}return Fl::run();
}

static bool open_test_viewport(Viewer& v){
    v.win.resize(40,40,800,540);v.win.show();Fl::check();
    return v.canvas.w()==800 && v.canvas.h()==461;
}
API int viewer_check(Open o,Read r,Save s,const char* first,const char* stack,const char* png){
    Fl::scheme("gtk+");Viewer v(o,r,s);if(!open_test_viewport(v))return 10;
    v.append(first);v.append(stack);v.append(first);
    if(v.items.size()!=2||!v.select(0)||!v.render())return 1;
    v.page(1,false);if(v.active!=1||v.count<2||!v.render())return 2;
    v.it().pose={v.tr().mid,12,0};v.refresh();if(!v.render())return 3;
    RGB plane=v.pixels;v.page(1,true);if(v.it().frame!=1||!v.render()||v.pixels==plane)return 4;
    if(s(png,v.pw,v.ph,v.pixels.data(),v.msg.data(),v.msg.size()))return 5;
    v.page(-1,false);v.page(-1,false);if(v.active!=0||v.it().frame!=0)return 6;
    v.page(1,false);v.page(1,false);if(v.active!=1||v.it().frame!=1)return 7;
    v.page(-1,true);v.page(-1,true);v.it().pose={v.tr().mid,12,0};v.refresh();
    return v.active==1&&v.it().frame==0&&v.render()&&plane==v.pixels?0:8;
}
#include "check_ndpi_zoom.hpp"
API int viewer_overlays(Open o,Read r,Save s,const char* stack,const char* folder){
    Fl::scheme("gtk+");Viewer v(o,r,s);v.append(stack);if(!v.select(0)||!v.mx||!v.my)return 1;
    if(!open_test_viewport(v))return 10;
    v.it().pose={v.tr().mid,12,0};v.refresh();
    v.finish_mark({40,65},false);v.finish_mark({130,65},true);v.finish_mark({130,140},false);
    if(v.it().marks.size()!=1||v.hit({80,65})!=0)return 2;
    double um=length(v.it().marks[0].points,v.mx,v.my);
    if(std::abs(um-90*v.mx-75*v.my)>1e-6)return 3;
    auto write=[&](const std::string& file,const RGB& rgb){
        auto dest=(fs::u8path(folder)/file).u8string();return !rgb.empty()&&!s(dest.c_str(),v.pw,v.ph,rgb.data(),v.msg.data(),v.msg.size());
    };
    for(int bar:{0,1})for(int marks:{0,1}){
        auto pix=v.compose(bar,marks);for(auto ext:{".png",".jpg"})
            if(!write(std::string("overlay-")+std::to_string(bar)+std::to_string(marks)+ext,pix))return 4;
    }
    v.rotate(90);auto t=v.tr();v.it().pose.center=Point{160.5,110.5}-t.inverse_delta({v.pw/2+.5-t.mid.x,v.ph/2+.5-t.mid.y});
    v.refresh();if(!write("rotated.png",v.compose(false,false)))return 5;
    v.rotate(37);auto p=v.tr().screen({80,65});if(v.hit(p)!=0)return 6;
    auto bar=scale(v.tr(),v.mx,v.my);auto delta=v.tr().inverse_delta({bar.px,0});
    if(std::abs(bar.um-std::hypot(delta.x*v.mx,delta.y*v.my))>1e-6||scale(v.tr(),0,v.my).px!=0)return 7;
    v.frame(1);if(v.hit(p)!=-1||v.compose(false,true)!=v.compose(false,false))return 8;
    v.frame(0);if(v.hit(p)!=0)return 9;
    v.win.hide();return 0;
}
API int viewer_details(Open o,Read r,Macro macro,const char* scan,const char* stack){
    Viewer v(o,r,nullptr);v.macro=macro;v.append(scan);v.append(stack);if(!v.select(0))return 1;
    auto b=v.glass();if(b.w!=100||b.h!=84)return 2;
    auto at=[&](int x,int y){return b.rgb.data()+(y*b.w+x)*3;};
    if(at(10,10)[1]!=180||at(0,50)[0]!=0||at(3,50)[0]!=255)return 3;
    if(v.info.find("PYRAMID")==std::string::npos||v.info.find("40x-reference pixel length")==std::string::npos)return 4;
    if(!v.select(1)||!v.glass().rgb.empty()||v.info.find("Frame/slice count:  3")==std::string::npos)return 5;
    return 0;
}
